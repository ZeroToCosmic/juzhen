from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ResolvedElement:
    locator: Any
    alias: str
    scope: str
    candidate: dict
    diagnostics: dict


class LocatorResolutionError(RuntimeError):
    def __init__(self, code: str, alias: str, scope: str, diagnostics: dict):
        self.code = code
        self.alias = alias
        self.scope = scope
        self.diagnostics = diagnostics
        super().__init__(f"{code}: {alias} ({scope})")


def _candidate_diagnostic(candidate: dict, raw_count: int, visible_count: int, actionable_count: int) -> dict:
    return {
        "id": candidate["id"],
        "type": candidate["type"],
        "raw_count": raw_count,
        "visible_count": visible_count,
        "actionable_count": actionable_count,
    }


async def _visible_indices(locator: Any) -> tuple[int, list[int]]:
    raw_count = await locator.count()
    visible = []
    for index in range(raw_count):
        if await locator.nth(index).is_visible():
            visible.append(index)
    return raw_count, visible


async def _is_uncovered_and_stable(locator: Any) -> bool:
    state = await locator.evaluate(
        """element => {
            if (!element || !element.isConnected) {
                return {connected: false, hidden: false, exiting: false, covered: false};
            }
            const style = getComputedStyle(element);
            const ancestors = [];
            for (let current = element; current; current = current.parentElement) {
                ancestors.push(current);
            }
            const classNames = ancestors.flatMap(current => Array.from(current.classList));
            const hasExitClass = classNames.some(name => /(?:^|[-_])(?:leave|exit|exiting)(?:[-_]|$)/i.test(name));
            const hasEnterActiveClass = classNames.some(name => /(?:^|[-_])enter-active(?:[-_]|$)/i.test(name));
            const hasEnterDoneClass = classNames.some(name => /(?:^|[-_])enter-done(?:[-_]|$)/i.test(name));
            const lifecycleExiting = hasExitClass || (hasEnterActiveClass && !hasEnterDoneClass);
            const hidden = ancestors.some(current => current.hidden || current.getAttribute('aria-hidden') === 'true'
                || current.hasAttribute('inert'))
                || style.display === 'none' || style.visibility !== 'visible'
                || style.pointerEvents === 'none' || Number.parseFloat(style.opacity) === 0;
            const exiting = lifecycleExiting || Boolean(element.closest(
                '[data-state="exiting"], [data-state="closing"], [data-state="closed"], '
                + '[data-phase="exiting"], [data-phase="leaving"]'
            ));
            const rect = element.getBoundingClientRect();
            const hit = rect.width > 0 && rect.height > 0
                ? document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
                : null;
            return {
                connected: true,
                hidden,
                exiting,
                covered: Boolean(hit && hit !== element && !element.contains(hit)),
            };
        }"""
    )
    return (
        isinstance(state, dict)
        and state.get("connected") is True
        and state.get("hidden") is False
        and state.get("exiting") is False
        and state.get("covered") is False
    )


async def _usable_indices(locator: Any) -> tuple[int, list[int], list[int]]:
    raw_count, visible_indices = await _visible_indices(locator)
    usable_indices = []
    for index in visible_indices:
        if await _is_uncovered_and_stable(locator.nth(index)):
            usable_indices.append(index)
    return raw_count, visible_indices, usable_indices


def _xpath_string_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    arguments = []
    for index, part in enumerate(parts):
        if part:
            arguments.append(f"'{part}'")
        if index < len(parts) - 1:
            arguments.append('"\'"')
    return f"concat({', '.join(arguments)})"


async def _resolve_active_video_scope(page: Any) -> tuple[Any, dict]:
    containers = page.locator("#column-list-container")
    container_count, visible_containers = await _visible_indices(containers)
    if len(visible_containers) != 1:
        raise LocatorResolutionError(
            "element_scope_not_found",
            "",
            "active_video",
            {"container_count": container_count, "visible_container_count": len(visible_containers)},
        )

    container = containers.nth(visible_containers[0])
    container_box = await container.bounding_box()
    if not container_box:
        raise LocatorResolutionError("element_scope_not_found", "", "active_video", {"container_box": "missing"})
    center_line = container_box["y"] + container_box["height"] / 2
    articles = container.locator('article[data-e2e="recommend-list-item-container"]')
    article_count, visible_articles = await _visible_indices(articles)
    intersecting = []
    for index in visible_articles:
        box = await articles.nth(index).bounding_box()
        if box and box["y"] <= center_line < box["y"] + box["height"]:
            intersecting.append(index)
    if len(intersecting) != 1:
        raise LocatorResolutionError(
            "element_scope_not_found",
            "",
            "active_video",
            {"article_count": article_count, "visible_article_count": len(visible_articles), "center_intersection_count": len(intersecting)},
        )

    article = articles.nth(intersecting[0])
    identifier = await article.get_attribute("id")
    if not identifier:
        raise LocatorResolutionError("element_scope_not_found", "", "active_video", {"scope_target": "missing_id"})
    matching_ids = 0
    for index in range(article_count):
        if await articles.nth(index).get_attribute("id") == identifier:
            matching_ids += 1
    if matching_ids != 1:
        raise LocatorResolutionError(
            "element_scope_not_found",
            "",
            "active_video",
            {"matching_article_id_count": matching_ids},
        )
    id_locator = page.locator(f"xpath=//*[@id={_xpath_string_literal(identifier)}]")
    if await id_locator.count() != 1:
        raise LocatorResolutionError(
            "element_scope_not_found",
            "",
            "active_video",
            {"matching_article_id_count": matching_ids},
        )
    return id_locator, {"scope_target": identifier}


async def _resolve_visible_comment_panel_scope(page: Any) -> tuple[Any, dict]:
    inputs = page.locator('[data-e2e="comment-input"]')
    input_count, visible_inputs, usable_inputs = await _usable_indices(inputs)
    if len(usable_inputs) != 1:
        raise LocatorResolutionError(
            "element_scope_not_found",
            "",
            "visible_comment_panel",
            {
                "input_count": input_count,
                "visible_input_count": len(visible_inputs),
                "usable_input_count": len(usable_inputs),
            },
        )

    panel = inputs.nth(usable_inputs[0]).locator("xpath=ancestor::section[1]")
    panel_count, visible_panels, usable_panels = await _usable_indices(panel)
    if len(usable_panels) != 1:
        raise LocatorResolutionError(
            "element_scope_not_found",
            "",
            "visible_comment_panel",
            {
                "panel_count": panel_count,
                "visible_panel_count": len(visible_panels),
                "usable_panel_count": len(usable_panels),
            },
        )
    return panel.nth(usable_panels[0]), {"scope_target": "visible_comment_panel"}


async def _resolve_scope_unchecked(page: Any, scope: str) -> tuple[Any, dict]:
    if scope == "page":
        return page, {"scope_target": "page"}
    if scope == "active_video":
        return await _resolve_active_video_scope(page)
    if scope == "visible_comment_panel":
        return await _resolve_visible_comment_panel_scope(page)
    raise LocatorResolutionError("element_scope_not_found", "", scope, {})


async def resolve_scope(page: Any, scope: str) -> tuple[Any, dict]:
    try:
        return await _resolve_scope_unchecked(page, scope)
    except LocatorResolutionError:
        raise
    except Exception:
        raise LocatorResolutionError(
            "element_resolution_failed",
            "",
            scope,
            {"phase": "scope_query"},
        ) from None


def role_locator(scope_locator: Any, candidate: dict) -> Any:
    return scope_locator.get_by_role(
        candidate["role"],
        name=candidate["name"],
        exact=candidate["name_mode"] == "exact",
    )


def apply_descendant(locator: Any, descendant: dict | None) -> Any:
    if not descendant:
        return locator
    return locator.locator(
        f'[{descendant["name"]}="{descendant["value"]}"]'
    )


def build_candidate_locator(scope_locator: Any, candidate: dict) -> Any:
    kind = candidate["type"]
    if kind == "css":
        return scope_locator.locator(candidate["value"])
    if kind == "xpath":
        return scope_locator.locator(f"xpath={candidate['value']}")
    if kind == "attribute":
        locator = scope_locator.locator(f'[{candidate["name"]}="{candidate["value"]}"]')
        return apply_descendant(locator, candidate.get("descendant"))
    if kind == "role":
        return role_locator(scope_locator, candidate)
    raise ValueError(f"unsupported locator type: {kind}")


async def _resolve_element(
    page: Any,
    alias: str,
    definition: dict,
    *,
    require_actionable: bool = True,
) -> ResolvedElement:
    scope = definition["scope"]
    scope_locator, scope_diagnostics = await resolve_scope(page, scope)
    diagnostics = {**scope_diagnostics, "candidates": []}
    for candidate in definition["locators"]:
        if not candidate["enabled"]:
            continue
        locator = build_candidate_locator(scope_locator, candidate)
        raw_count, visible_indices, usable_indices = (
            await _usable_indices(locator)
        )
        actionable_indices = [
            index for index in usable_indices
            if await locator.nth(index).is_enabled()
        ]
        qualified_indices = (
            actionable_indices
            if require_actionable
            else visible_indices
        )
        candidate_diagnostics = _candidate_diagnostic(
            candidate, raw_count, len(visible_indices), len(actionable_indices)
        )
        diagnostics["candidates"].append(candidate_diagnostics)
        if len(qualified_indices) > 1:
            raise LocatorResolutionError("element_candidate_ambiguous", alias, scope, diagnostics)
        if len(qualified_indices) == 1:
            return ResolvedElement(
                locator=locator.nth(qualified_indices[0]),
                alias=alias,
                scope=scope,
                candidate={"id": candidate["id"], "type": candidate["type"]},
                diagnostics=diagnostics,
            )
    raise LocatorResolutionError("element_candidate_not_found", alias, scope, diagnostics)


async def resolve_element(page: Any, alias: str, definition: dict) -> ResolvedElement:
    scope = definition.get("scope", "") if isinstance(definition, dict) else ""
    try:
        return await _resolve_element(
            page,
            alias,
            definition,
            require_actionable=True,
        )
    except LocatorResolutionError:
        raise
    except Exception:
        raise LocatorResolutionError(
            "element_resolution_failed",
            alias,
            scope,
            {"phase": "locator_query"},
        ) from None


async def resolve_visible_element(
    page: Any,
    alias: str,
    definition: dict,
) -> ResolvedElement:
    scope = (
        definition.get("scope", "")
        if isinstance(definition, dict)
        else ""
    )
    try:
        return await _resolve_element(
            page,
            alias,
            definition,
            require_actionable=False,
        )
    except LocatorResolutionError:
        raise
    except Exception:
        raise LocatorResolutionError(
            "element_resolution_failed",
            alias,
            scope,
            {"phase": "locator_query"},
        ) from None


async def _inspect_with(
    resolver: Any,
    page: Any,
    alias: str,
    definition: dict,
) -> dict:
    try:
        resolved = await resolver(page, alias, definition)
    except LocatorResolutionError as error:
        return {
            "status": "error",
            "code": error.code,
            "alias": error.alias,
            "scope": error.scope,
            "diagnostics": error.diagnostics,
        }
    except Exception:
        return {
            "status": "error",
            "code": "element_inspection_failed",
            "alias": alias,
            "scope": definition.get("scope", "") if isinstance(definition, dict) else "",
            "diagnostics": {"phase": "inspection"},
        }
    return {
        "status": "ok",
        "alias": resolved.alias,
        "scope": resolved.scope,
        "candidate": resolved.candidate,
        "diagnostics": resolved.diagnostics,
    }


async def inspect_element(page: Any, alias: str, definition: dict) -> dict:
    return await _inspect_with(
        resolve_element,
        page,
        alias,
        definition,
    )


async def inspect_visible_element(
    page: Any,
    alias: str,
    definition: dict,
) -> dict:
    return await _inspect_with(
        resolve_visible_element,
        page,
        alias,
        definition,
    )
