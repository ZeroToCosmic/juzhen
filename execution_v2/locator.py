"""Strict, public-API-only resolution for manually picked V2 elements."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any


_STABLE_ATTRIBUTE = re.compile(
    r'''\[(?:data-e2e|data-testid|aria-label|name|placeholder|role|contenteditable)'''
    r'''\s*=\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\]'''
)


def _stable_css_chain(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(match.group(0) for match in _STABLE_ATTRIBUTE.finditer(value))


class LocatorResolutionError(RuntimeError):
    """A saved locator did not resolve to one actionable DOM node."""

    def __init__(self, code: str, diagnostics: tuple[dict[str, Any], ...] = ()):
        self.code = code
        self.diagnostics = diagnostics
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ResolvedElement:
    handle: Any
    locator_type: str
    box: dict[str, float]
    diagnostics: tuple[dict[str, Any], ...]

    @property
    def bounding_box(self) -> dict[str, float]:
        return self.box


class StrictLocatorResolver:
    """Resolve every saved candidate, accepting only one exact actionable node."""

    async def resolve(
        self,
        page: Any,
        definition: dict[str, Any],
        *,
        require_editable: bool = False,
        require_in_viewport: bool = False,
        allow_viewport_fallback: bool = False,
    ) -> ResolvedElement:
        frame = await self._resolve_frame(page, definition.get("frame_path", []))
        candidates = definition.get("locators", [])
        viewport = None
        if require_in_viewport:
            viewport = await page.evaluate(
                "({width: window.innerWidth, height: window.innerHeight})"
            )
        diagnostics: list[dict[str, Any]] = []
        passing: list[tuple[dict[str, Any], Any, dict[str, float]]] = []

        for candidate in sorted(
            enumerate(candidates), key=lambda entry: (entry[1].get("priority", 0), entry[0])
        ):
            _, locator_definition = candidate
            result = await self._validate_candidate(
                frame,
                locator_definition,
                require_editable=require_editable,
                viewport=viewport,
            )
            diagnostics.append(result[0])
            if result[1] is not None:
                passing.append((locator_definition, result[1], result[2]))

        if not passing:
            if allow_viewport_fallback and viewport is not None:
                try:
                    fallback = await self._viewport_fallback(
                        frame,
                        candidates,
                        viewport,
                        require_editable=require_editable,
                    )
                except LocatorResolutionError as error:
                    raise LocatorResolutionError(
                        error.code, tuple(diagnostics) + error.diagnostics
                    ) from error
                return ResolvedElement(
                    fallback.handle,
                    fallback.locator_type,
                    fallback.box,
                    tuple(diagnostics) + fallback.diagnostics,
                )
            raise LocatorResolutionError("no_valid_locator", tuple(diagnostics))

        reference_handle = passing[0][1]
        for _, handle, _ in passing[1:]:
            if not await self._same_handle(frame, reference_handle, handle):
                raise LocatorResolutionError("locator_conflict", tuple(diagnostics))

        selected, handle, box = passing[0]
        return ResolvedElement(
            handle=handle,
            locator_type=selected["type"],
            box=box,
            diagnostics=tuple(diagnostics),
        )

    async def _resolve_frame(self, page: Any, frame_path: list[str]) -> Any:
        frame = page
        for selector in frame_path:
            try:
                iframe_locator = frame.locator(selector)
                count = await iframe_locator.count()
            except Exception as error:
                raise LocatorResolutionError(
                    "frame_path_invalid", ({"code": "frame_lookup_failed", "selector": selector},)
                ) from error
            if count != 1:
                code = "frame_not_found" if count == 0 else "frame_not_unique"
                raise LocatorResolutionError(
                    "frame_path_invalid", ({"code": code, "selector": selector, "count": count},)
                )
            iframe_handle = await iframe_locator.element_handle()
            content_frame = getattr(iframe_handle, "content_frame", None)
            if iframe_handle is None or not callable(content_frame):
                raise LocatorResolutionError(
                    "frame_path_invalid", ({"code": "frame_handle_missing", "selector": selector},)
                )
            child_frame = content_frame()
            if inspect.isawaitable(child_frame):
                child_frame = await child_frame
            if child_frame is None:
                raise LocatorResolutionError(
                    "frame_path_invalid", ({"code": "frame_unavailable", "selector": selector},)
                )
            frame = child_frame
        return frame

    async def _validate_candidate(
        self,
        frame: Any,
        candidate: dict[str, Any],
        *,
        require_editable: bool,
        viewport: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], Any | None, dict[str, float] | None]:
        locator_type = candidate.get("type")
        diagnostic: dict[str, Any] = {"locator_type": locator_type, "priority": candidate.get("priority")}
        try:
            locator = self._create_locator(frame, candidate)
        except (KeyError, TypeError, ValueError):
            diagnostic["code"] = "locator_invalid"
            return diagnostic, None, None
        try:
            count = await locator.count()
        except Exception:
            diagnostic["code"] = "locator_query_failed"
            return diagnostic, None, None
        if count != 1:
            diagnostic["code"] = "locator_not_found" if count == 0 else "locator_not_unique"
            diagnostic["count"] = count
            return diagnostic, None, None
        try:
            handle = await locator.element_handle()
            if handle is None:
                diagnostic["code"] = "locator_handle_missing"
                return diagnostic, None, None
            if not await handle.is_visible():
                diagnostic["code"] = "locator_not_visible"
                return diagnostic, None, None
            box = await handle.bounding_box()
            if not self._has_area(box):
                diagnostic["code"] = "locator_zero_box"
                return diagnostic, None, None
            if await handle.is_disabled():
                diagnostic["code"] = "locator_disabled"
                return diagnostic, None, None
            if require_editable and not await handle.is_editable():
                diagnostic["code"] = "locator_not_editable"
                return diagnostic, None, None
            if viewport is not None and not self._center_in_viewport(box, viewport):
                diagnostic["code"] = "locator_outside_viewport"
                return diagnostic, None, None
        except Exception:
            diagnostic["code"] = "locator_state_check_failed"
            return diagnostic, None, None
        normalized_box = {
            "x": box["x"],
            "y": box["y"],
            "width": box["width"],
            "height": box["height"],
        }
        diagnostic["code"] = "valid"
        return diagnostic, handle, normalized_box

    async def _viewport_fallback(
        self,
        frame: Any,
        candidates: list[dict[str, Any]],
        viewport: dict[str, Any],
        *,
        require_editable: bool,
    ) -> ResolvedElement:
        passing: list[tuple[Any, dict[str, float]]] = []
        diagnostics: list[dict[str, Any]] = []
        seen_chains: set[str] = set()
        saw_ambiguous = False

        for _, candidate in sorted(
            enumerate(candidates),
            key=lambda entry: (entry[1].get("priority", 0), entry[0]),
        ):
            if candidate.get("type") != "css":
                continue
            chain = _stable_css_chain(candidate.get("value"))
            if not chain or chain in seen_chains:
                continue
            seen_chains.add(chain)
            diagnostic: dict[str, Any] = {
                "locator_type": "css_viewport",
                "value": chain,
            }
            try:
                locator = frame.locator(chain)
                count = await locator.count()
            except Exception:
                diagnostic["code"] = "locator_query_failed"
                diagnostics.append(diagnostic)
                continue

            handles: list[tuple[Any, dict[str, float]]] = []
            for index in range(count):
                try:
                    handle = await locator.nth(index).element_handle()
                    if handle is None or not await handle.is_visible():
                        continue
                    box = await handle.bounding_box()
                    if not self._has_area(box) or not self._center_in_viewport(
                        box, viewport
                    ):
                        continue
                    if await handle.is_disabled():
                        continue
                    if require_editable and not await handle.is_editable():
                        continue
                except Exception:
                    continue
                handles.append(
                    (
                        handle,
                        {
                            "x": box["x"],
                            "y": box["y"],
                            "width": box["width"],
                            "height": box["height"],
                        },
                    )
                )

            if not handles:
                diagnostic["code"] = "current_viewport_target_not_found"
            elif len(handles) > 1:
                diagnostic["code"] = "current_viewport_target_ambiguous"
                saw_ambiguous = True
            else:
                diagnostic["code"] = "valid"
                passing.append(handles[0])
            diagnostics.append(diagnostic)

        if not passing:
            code = (
                "current_viewport_target_ambiguous"
                if saw_ambiguous
                else "current_viewport_target_not_found"
            )
            raise LocatorResolutionError(code, tuple(diagnostics))

        reference, box = passing[0]
        for handle, _ in passing[1:]:
            if not await self._same_handle(frame, reference, handle):
                raise LocatorResolutionError(
                    "current_viewport_target_ambiguous", tuple(diagnostics)
                )
        return ResolvedElement(reference, "css_viewport", box, tuple(diagnostics))

    @staticmethod
    def _create_locator(frame: Any, candidate: dict[str, Any]) -> Any:
        locator_type = candidate["type"]
        if locator_type == "css":
            return frame.locator(candidate["value"])
        if locator_type == "xpath":
            return frame.locator(f"xpath={candidate['value']}")
        if locator_type == "role":
            return frame.get_by_role(candidate["role"], name=candidate["name"], exact=True)
        raise ValueError("unsupported locator type")

    @staticmethod
    def _has_area(box: Any) -> bool:
        return isinstance(box, dict) and box.get("width", 0) > 0 and box.get("height", 0) > 0

    @staticmethod
    def _center_in_viewport(box: dict[str, Any], viewport: dict[str, Any]) -> bool:
        center_x = float(box["x"]) + float(box["width"]) / 2
        center_y = float(box["y"]) + float(box["height"]) / 2
        return (
            0 <= center_x < float(viewport["width"])
            and 0 <= center_y < float(viewport["height"])
        )

    @staticmethod
    async def _same_handle(frame: Any, left: Any, right: Any) -> bool:
        if left is right:
            return True
        try:
            return bool(await frame.evaluate("(pair) => pair[0] === pair[1]", [left, right]))
        except Exception:
            try:
                return bool(await left.evaluate("(element, other) => element === other", right))
            except Exception as error:
                raise LocatorResolutionError(
                    "locator_comparison_failed", ({"code": "handle_comparison_failed"},)
                ) from error
