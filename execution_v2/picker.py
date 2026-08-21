"""Explicit, short-lived manual picker sessions for browser execution V2."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .locator import StrictLocatorResolver
from .models import BrowserBinding


class PickerError(RuntimeError):
    """A picker session cannot safely accept or store a selection."""


_OVERLAY_SOURCE = Path(__file__).with_name("picker_overlay.js").read_text(encoding="utf-8")
_UNINSTALL = "window.__executionV2Picker && window.__executionV2Picker.uninstall();"
_SAFE_ATTRIBUTES = ("data-e2e", "data-testid", "data-test", "data-qa", "aria-label", "role", "name", "placeholder", "id", "contenteditable")
_STABLE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_PURPOSES = {"action", "readiness"}
_KINDS = {"click", "input", "generic"}


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _safe_box(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        item = value.get(key, 0)
        result[key] = float(item) if isinstance(item, (int, float)) and not isinstance(item, bool) else 0
    return result


def _unique_selector_bundle() -> str:
    """Bundle the installed CommonJS package for the page without a build step."""

    project_root = Path(__file__).resolve().parent.parent
    package_root = project_root / "node_modules" / "@cypress" / "unique-selector" / "lib"
    escape_path = project_root / "node_modules" / "css.escape" / "css.escape.js"
    files = sorted(package_root.glob("*.js")) if package_root.is_dir() else []
    if not files or not escape_path.is_file():
        raise PickerError("picker_unique_selector_dependency_missing")

    modules: list[str] = []
    for path in files:
        module_id = f"@cypress/unique-selector/lib/{path.stem}"
        modules.append(
            f"{json.dumps(module_id)}:function(module,exports,require,window){{\n"
            f"{path.read_text(encoding='utf-8')}\n}}"
        )
    modules.append(
        '"css.escape":function(module,exports,require,window){\n'
        "var global = window;\n"
        f"{escape_path.read_text(encoding='utf-8')}\n}}"
    )
    return """(function(window) {
  "use strict";
  var modules = {""" + ",\n".join(modules) + """};
  var cache = {};
  function resolve(parent, request) {
    if (request.charAt(0) !== ".") return request;
    var parts = parent.split("/");
    parts.pop();
    request.split("/").forEach(function(part) {
      if (!part || part === ".") return;
      if (part === "..") parts.pop(); else parts.push(part);
    });
    return parts.join("/");
  }
  function load(id) {
    if (cache[id]) return cache[id].exports;
    if (!modules[id]) throw new Error("missing picker module: " + id);
    var module = { exports: {} };
    cache[id] = module;
    modules[id](module, module.exports, function(request) { return load(resolve(id, request)); }, window);
    return module.exports;
  }
  var exported = load("@cypress/unique-selector/lib/index");
  window.__executionV2UniqueSelector = exported.default || exported;
})(window);"""


def sanitize_picker_payload(value: object) -> dict[str, Any] | None:
    """Keep only a small, non-secret event record from the page overlay."""

    if not isinstance(value, Mapping) or value.get("type", "selection") != "selection":
        return None
    raw_attrs = value.get("attributes")
    attrs: dict[str, str] = {}
    if isinstance(raw_attrs, Mapping):
        for name in _SAFE_ATTRIBUTES:
            raw_value = raw_attrs.get(name)
            if not isinstance(raw_value, str):
                continue
            cleaned = _clean(raw_value, 160)
            if cleaned or (name == "contenteditable" and name in raw_attrs):
                attrs[name] = cleaned
    frame_path = value.get("frame_path")
    if not isinstance(frame_path, list) or any(not _clean(item, 400) for item in frame_path):
        frame_path = []
    return {
        "tag": _clean(value.get("actionable_tag") or value.get("tag"), 40).lower(),
        "original_tag": _clean(value.get("original_tag") or value.get("tag"), 40).lower(),
        "attributes": attrs,
        "role": _clean(value.get("role"), 80),
        "name": _clean(value.get("name"), 160),
        "text_preview": _clean(value.get("text_preview"), 240),
        "frame_path": [_clean(item, 400) for item in frame_path],
        "original_fingerprint": _clean(value.get("original_fingerprint"), 220),
        "actionable_ancestor_fingerprint": _clean(value.get("actionable_ancestor_fingerprint"), 220),
        "bounding_box": _safe_box(value.get("bounding_box")),
        "unique_css": _clean(value.get("unique_css"), 1200),
        "relative_xpath": _clean(value.get("relative_xpath"), 1200),
    }


def _css_attr(name: str, value: str) -> str:
    return f"[{name}={json.dumps(value, ensure_ascii=False)}]"


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def generate_locator_candidates(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Generate ordered, selector-only candidates from one user-selected node."""

    attrs = selection.get("attributes") if isinstance(selection.get("attributes"), Mapping) else {}
    tag = _clean(selection.get("tag"), 40).lower() or "*"
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(locator_type: str, value: str, priority: int) -> None:
        key = (locator_type, value)
        if value and key not in seen:
            candidates.append({"type": locator_type, "value": value, "priority": priority})
            seen.add(key)

    for priority, name in enumerate(("data-e2e", "data-testid", "data-test", "data-qa"), start=10):
        if value := _clean(attrs.get(name), 160):
            add("css", _css_attr(name, value), priority)
    if value := _clean(attrs.get("aria-label"), 160):
        add("css", _css_attr("aria-label", value), 20)
    if "contenteditable" in attrs:
        add("css", _css_attr("contenteditable", str(attrs["contenteditable"])), 21)
    role, name = _clean(selection.get("role"), 80), _clean(selection.get("name"), 160)
    if role and name:
        candidates.append({"type": "role", "role": role, "name": name, "priority": 30})
    for priority, name in ((40, "id"), (41, "name"), (42, "placeholder")):
        value = _clean(attrs.get(name), 160)
        if not value or (name == "id" and not _STABLE_ID.fullmatch(value)):
            continue
        add("css", f"#{value}" if name == "id" else _css_attr(name, value), priority)
    unique_css = _clean(selection.get("unique_css"), 1200)
    if unique_css:
        add("css", unique_css, 50)
    text = _clean(selection.get("text_preview"), 160)
    if text:
        add("xpath", f"//{tag}[normalize-space(.)={_xpath_literal(text)}]", 60)
    xpath = _clean(selection.get("relative_xpath"), 1200)
    if xpath and not xpath.startswith("//"):
        xpath = ""
    if xpath and not re.search(r"/(?:html|body)(?:/|$)", xpath, re.IGNORECASE):
        add("xpath", xpath, 70)
    return sorted(candidates, key=lambda item: item["priority"])


class PickerSession:
    """One page-bound session; only ``finish`` or ``cancel`` closes its profile."""

    def __init__(
        self,
        binding: BrowserBinding,
        target_url: str,
        *,
        resolver: StrictLocatorResolver,
        close: Callable[[], object] | None,
    ) -> None:
        self.binding = binding
        self.target_url = target_url
        self._resolver = resolver
        self._close = close
        self._pending: deque[dict[str, Any]] = deque()
        self._selected: dict[str, Any] | None = None
        self._event = asyncio.Event()
        self._active = True
        self._selections: list[dict[str, Any]] = []

    @property
    def selections(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._selections)

    async def start(self) -> None:
        async def receive(_source: object, payload: object = None) -> None:
            # Playwright invokes exposed bindings as (source, payload); the fallback
            # keeps simple in-process test adapters compatible.
            self._accept(payload if payload is not None else _source)

        await self.binding.page.expose_binding("__executionV2PickerEvent", receive)
        install = _unique_selector_bundle() + "\n" + _OVERLAY_SOURCE + "\nwindow.__executionV2Picker.install();"
        await self.binding.page.add_init_script(install)
        await self.binding.page.evaluate(install)

    def _accept(self, payload: object) -> None:
        if not self._active:
            return
        if isinstance(payload, Mapping) and payload.get("type") == "cancel":
            asyncio.get_running_loop().create_task(self.cancel())
            return
        clean = sanitize_picker_payload(payload)
        if clean is not None:
            self._pending.append(clean)
            self._event.set()

    async def next_selection(self) -> dict[str, Any]:
        if not self._active:
            raise PickerError("picker_not_active")
        while not self._pending:
            self._event.clear()
            await self._event.wait()
            if not self._active:
                raise PickerError("picker_not_active")
        self._selected = self._pending.popleft()
        return dict(self._selected)

    async def save_selection(self, name: str, purpose: str, kind: str) -> dict[str, Any]:
        if not self._active:
            raise PickerError("picker_not_active")
        display_name = _clean(name, 120)
        if not display_name or purpose not in _PURPOSES or kind not in _KINDS:
            raise PickerError("picker_selection_invalid")
        if self._selected is None:
            raise PickerError("picker_selection_required")
        locators = generate_locator_candidates(self._selected)
        if not locators:
            raise PickerError("picker_locator_missing")
        definition = {
            "url_pattern": self.target_url,
            "frame_path": list(self._selected["frame_path"]),
            "locators": locators,
            "diagnostic_metadata": {
                "tag": self._selected["tag"],
                "original_tag": self._selected["original_tag"],
                "attributes": dict(self._selected["attributes"]),
                "role": self._selected["role"],
                "name": self._selected["name"],
                "text_preview": self._selected["text_preview"],
                "original_fingerprint": self._selected["original_fingerprint"],
                "actionable_ancestor_fingerprint": self._selected["actionable_ancestor_fingerprint"],
                "bounding_box": dict(self._selected["bounding_box"]),
            },
            "screenshot_path": "",
        }
        try:
            await self._resolver.resolve(
                self.binding.page, definition, require_editable=(kind == "input")
            )
        except Exception as error:
            code = (
                "picker_input_target_not_editable"
                if kind == "input"
                else "picker_locator_invalid"
            )
            raise PickerError(code) from error
        saved = {"name": display_name, "purpose": purpose, "kind": kind, "definition": definition}
        self._selections.append(saved)
        self._selected = None
        return saved

    async def finish(self) -> tuple[dict[str, Any], ...]:
        await self._close_session()
        return self.selections

    async def cancel(self) -> None:
        await self._close_session()

    async def _close_session(self) -> None:
        if not self._active:
            return
        self._active = False
        self._event.set()
        try:
            await self.binding.page.evaluate(_UNINSTALL)
        finally:
            if self._close is not None:
                result = self._close()
                if inspect.isawaitable(result):
                    await result


class PickerService:
    """Create only explicitly requested picker sessions for Phase 1 bindings."""

    def __init__(self, *, resolver: StrictLocatorResolver | None = None) -> None:
        self._resolver = resolver or StrictLocatorResolver()

    async def start(
        self,
        binding: BrowserBinding,
        target_url: str,
        *,
        close: Callable[[], object] | None = None,
    ) -> PickerSession:
        if not isinstance(binding, BrowserBinding):
            raise TypeError("binding must be a BrowserBinding")
        if not isinstance(target_url, str) or not target_url.startswith("https://"):
            raise PickerError("picker_target_url_invalid")
        session = PickerSession(binding, target_url, resolver=self._resolver, close=close)
        await session.start()
        return session


__all__ = [
    "PickerError",
    "PickerService",
    "PickerSession",
    "generate_locator_candidates",
    "sanitize_picker_payload",
]
