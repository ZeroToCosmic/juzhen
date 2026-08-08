"""Campaign-specific locator checks built exclusively on V2 strict locators."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from .errors import CampaignValidationError
from .receipts import normalize_comment_text
from .video import normalize_tiktok_video
from execution_v2.locator import LocatorResolutionError, StrictLocatorResolver


@dataclass(frozen=True, slots=True)
class LocateLimits:
    timeout_seconds: float = 30
    max_scrolls: int = 12


async def locate_parent_comment(page: Any, receipt: dict[str, Any], limits: LocateLimits = LocateLimits()) -> Any:
    """Find one visible parent using receipt facts, never a page-wide Reply button."""
    if (type(limits.timeout_seconds) not in {int, float} or type(limits.max_scrolls) is not int
            or not 0 < limits.timeout_seconds <= 30 or not 0 <= limits.max_scrolls <= 12):
        raise ValueError("invalid parent locator limits")
    deadline = monotonic() + limits.timeout_seconds
    last: list[Any] = []
    for attempt in range(limits.max_scrolls + 1):
        candidates = await _comment_candidates(page)
        matches = _parent_matches(candidates, receipt)
        if len(matches) == 1:
            return await _materialize_parent(page, matches[0], receipt)
        if len(matches) > 1:
            raise CampaignValidationError("parent_comment_ambiguous")
        last = matches
        if attempt == limits.max_scrolls or monotonic() >= deadline:
            break
        scroll = getattr(page, "scroll_comment_panel", None)
        if not callable(scroll):
            break
        value = scroll()
        if hasattr(value, "__await__"):
            await value
        await asyncio.sleep(0)
    raise CampaignValidationError("parent_comment_ambiguous" if len(last) > 1 else "parent_comment_not_found")


async def open_scoped_reply(parent_node: Any, expected_author: str) -> Any:
    """Click and verify a reply composer belonging to this exact parent node."""
    matched_evidence: dict[str, Any] = {}
    if isinstance(parent_node, dict) and "node" in parent_node:
        matched_evidence = dict(parent_node.get("evidence") or {})
        parent_node = parent_node["node"]
    if isinstance(parent_node, dict):
        author = str(parent_node.get("author_username") or "").lstrip("@").casefold()
        if author != str(expected_author).lstrip("@").casefold():
            raise CampaignValidationError("reply_target_mismatch")
        reply = parent_node.get("reply_composer")
        if reply is None:
            raise CampaignValidationError("reply_target_mismatch")
        click = parent_node.get("reply_click")
        if callable(click):
            value = click()
            if hasattr(value, "__await__"):
                await value
        if str(parent_node.get("composer_target") or expected_author).lstrip("@").casefold() != str(expected_author).lstrip("@").casefold():
            raise CampaignValidationError("reply_target_mismatch")
        if not isinstance(reply, dict) or "input" not in reply or "submit" not in reply:
            raise CampaignValidationError("reply_target_mismatch")
        return {**reply, "parent_author": expected_author, "parent_node": parent_node, "parent_scope": _parent_scope(parent_node)}
    try:
        reply = await _unique_direct_control(
            parent_node, '[data-e2e="comment-reply"], button[aria-label="Reply"]'
        )
        await reply.click()
        composer = await _unique_direct_control(
            parent_node, '[contenteditable="true"], textarea'
        )
        container = composer.locator("xpath=ancestor::*[@data-e2e='reply-composer' or self::form][1]")
        if await container.count() != 1:
            raise CampaignValidationError("reply_target_mismatch")
        author = await composer.evaluate("node => String(node.getAttribute('data-reply-to') || node.closest('[data-reply-to]')?.getAttribute('data-reply-to') || node.getAttribute('aria-label') || '')")
        actual = str(author).strip().lstrip("@").casefold()
        expected = str(expected_author).strip().lstrip("@").casefold()
        if not expected or (actual != expected and expected not in actual.split()):
            raise CampaignValidationError("reply_target_mismatch")
        submit = container.locator('[data-e2e="comment-post"], button[type="submit"]')
        if await submit.count() != 1:
            raise CampaignValidationError("reply_target_mismatch")
        return {"input": composer, "submit": submit, "parent_author": str(author).strip(), "parent_node": parent_node, "parent_scope": _parent_scope(matched_evidence)}
    except CampaignValidationError:
        raise
    except Exception as exc:
        raise CampaignValidationError("reply_target_mismatch") from exc


async def _unique_direct_control(parent_node: Any, selector: str) -> Any:
    """Return one control whose nearest comment container is parent_node."""
    parent_handle = await parent_node.element_handle()
    if parent_handle is None:
        raise CampaignValidationError("reply_target_mismatch")
    candidates = parent_node.locator(selector)
    direct: list[Any] = []
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        is_direct = await candidate.evaluate(
            "(node, parent) => node.closest('[data-e2e=\"comment-item\"], [data-comment-id]') === parent",
            parent_handle,
        )
        if is_direct:
            direct.append(candidate)
    if len(direct) != 1:
        raise CampaignValidationError("reply_target_mismatch")
    return direct[0]


async def _comment_candidates(page: Any) -> list[Any]:
    source = getattr(page, "comment_candidates", None)
    if callable(source):
        value = source()
        if hasattr(value, "__await__"):
            value = await value
        return list(value) if isinstance(value, list) else []
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return []
    value = evaluate("""() => Array.from(document.querySelectorAll('[data-e2e="comment-item"], [data-comment-id]')).map((node, index) => {
      const style=getComputedStyle(node), rect=node.getBoundingClientRect();
      const author=node.querySelector('a[href*="/@"]');
      return {visible: style.display!=='none' && style.visibility!=='hidden' && rect.width>0 && rect.height>0,
        platform_comment_id: node.getAttribute('data-comment-id') || node.getAttribute('data-e2e-comment-id') || '',
        comment_permalink: node.querySelector('a[href*="comment"]')?.href || '',
        author_profile_href: author?.href || '', author_username: author?.textContent || '',
        text: node.querySelector('[data-e2e="comment-level-1"]')?.textContent || node.textContent || '',
        observed_at: new Date().toISOString(),
        stable_attributes: {comment_id: node.getAttribute('data-comment-id') || node.getAttribute('data-e2e-comment-id') || '', index, e2e: node.getAttribute('data-e2e') || ''}})""")
    if hasattr(value, "__await__"):
        value = await value
    if not isinstance(value, list):
        return []
    try:
        video_id = normalize_tiktok_video(str(page.url)).video_id
    except Exception:
        video_id = ""
    return [{**item, "video_id": video_id} for item in value if isinstance(item, dict)]


def _parent_matches(candidates: list[Any], receipt: dict[str, Any]) -> list[Any]:
    visible = [item for item in candidates if isinstance(item, dict) and item.get("visible") is True]
    for key in ("platform_comment_id", "comment_permalink"):
        identifier = receipt.get(key)
        if identifier:
            matches = [item for item in visible if item.get(key) == identifier]
            if matches:
                return matches
    text = normalize_comment_text(str(receipt.get("normalized_text") or ""))
    video = receipt.get("video_id")
    href = receipt.get("author_profile_href")
    if href:
        matches = [item for item in visible if item.get("author_profile_href") == href and normalize_comment_text(str(item.get("text") or "")) == text and (not video or item.get("video_id") == video)]
        if matches: return matches
    username = str(receipt.get("expected_username") or "").lstrip("@").casefold()
    if username:
        started = str(receipt.get("posting_window_started_at") or "")
        matches = [item for item in visible if str(item.get("author_username") or "").lstrip("@").casefold() == username and normalize_comment_text(str(item.get("text") or "")) == text and (not video or item.get("video_id") == video) and (not started or str(item.get("observed_at") or "") >= started)]
        if matches: return matches
    stable = receipt.get("stable_attributes")
    if isinstance(stable, dict):
        return [item for item in visible if isinstance(item.get("stable_attributes"), dict) and all(item["stable_attributes"].get(key) == value for key, value in stable.items())]
    return []


async def _materialize_parent(page: Any, candidate: Any, receipt: dict[str, Any]) -> Any:
    """Real DOM matches are rebuilt from a stable ID and rechecked once."""
    if not isinstance(candidate, dict):
        return candidate
    if callable(getattr(page, "comment_candidates", None)):
        return candidate
    locator_factory = getattr(page, "locator", None)
    if not callable(locator_factory) and callable(getattr(page, "comment_candidates", None)):
        return candidate
    if not callable(locator_factory):
        raise CampaignValidationError("parent_comment_not_found")
    identifier = candidate.get("platform_comment_id")
    stable = candidate.get("stable_attributes")
    try:
        if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", identifier):
            locator = locator_factory(f'[data-comment-id="{identifier}"], [data-e2e-comment-id="{identifier}"]')
        elif isinstance(stable, dict) and type(stable.get("index")) is int and 0 <= stable["index"] <= 500:
            locator = locator_factory('[data-e2e="comment-item"], [data-comment-id]').nth(stable["index"])
        else:
            raise CampaignValidationError("parent_comment_not_found")
        if await locator.count() != 1:
            raise CampaignValidationError("parent_comment_ambiguous")
        if not await locator.is_visible():
            raise CampaignValidationError("parent_comment_not_found")
        fresh = await locator.evaluate("""node => { const author=node.querySelector('a[href*="/@"]'); const nodes=Array.from(document.querySelectorAll('[data-e2e="comment-item"], [data-comment-id]')); return {
          visible:true, platform_comment_id:node.getAttribute('data-comment-id') || node.getAttribute('data-e2e-comment-id') || '',
          comment_permalink:node.querySelector('a[href*="comment"]')?.href || '', author_profile_href:author?.href || '',
          author_username:author?.textContent || '', text:node.querySelector('[data-e2e="comment-level-1"]')?.textContent || node.textContent || '',
          observed_at:new Date().toISOString(), stable_attributes:{comment_id:node.getAttribute('data-comment-id') || node.getAttribute('data-e2e-comment-id') || '', index:nodes.indexOf(node), e2e:node.getAttribute('data-e2e') || ''}} }""")
        if not isinstance(fresh, dict):
            raise CampaignValidationError("parent_comment_not_found")
        try:
            fresh["video_id"] = normalize_tiktok_video(str(page.url)).video_id
        except Exception:
            fresh["video_id"] = ""
        if len(_parent_matches([fresh], receipt)) != 1:
            raise CampaignValidationError("parent_comment_not_found")
        return {"node": locator, "evidence": fresh}
    except CampaignValidationError:
        raise
    except Exception as exc:
        raise CampaignValidationError("parent_comment_not_found") from exc


def _parent_scope(evidence: dict[str, Any]) -> dict[str, Any]:
    stable = evidence.get("stable_attributes")
    return {
        "parent_platform_comment_id": str(evidence.get("platform_comment_id") or ""),
        "parent_comment_permalink": str(evidence.get("comment_permalink") or ""),
        "parent_stable_attributes": dict(stable) if isinstance(stable, dict) else {},
    }


async def verify_video(page: Any, expected_video_id: str) -> dict[str, str]:
    """Require both the exact URL and a visible in-page link to that video."""
    try:
        current = normalize_tiktok_video(str(page.url))
    except Exception as exc:
        raise CampaignValidationError("target_video_mismatch") from exc
    if current.video_id != str(expected_video_id):
        raise CampaignValidationError("target_video_mismatch")
    try:
        hrefs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/video/"]'))
                .filter(node => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                        rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight;
                }).map(node => String(node.href || ''))"""
        )
        visible = next(
            normalize_tiktok_video(str(href)).canonical_url
            for href in (hrefs if isinstance(hrefs, list) else [])
            if normalize_tiktok_video(str(href)).video_id == str(expected_video_id)
        )
    except Exception as exc:
        raise CampaignValidationError("target_video_mismatch") from exc
    return {"canonical_url": current.canonical_url, "visible_video_href": visible}


async def verify_logged_in_username(page: Any, expected_username: str, account_definition: dict[str, Any], *, resolver: Any | None = None) -> dict[str, str]:
    resolved = await _resolve(page, account_definition, resolver=resolver)
    handle = getattr(resolved, "handle", resolved)
    try:
        value = await handle.evaluate("element => ({text: String(element.innerText || element.textContent || ''), href: String(element.href || '')})")
    except Exception as exc:
        raise CampaignValidationError("profile_identity_mismatch") from exc
    username = str(expected_username).strip().lstrip("@").casefold()
    actual = " ".join(str(value.get("text", "")).split()).lstrip("@").casefold() if isinstance(value, dict) else ""
    href = str(value.get("href", "")).casefold() if isinstance(value, dict) else ""
    if not username or (actual != username and f"/@{username}" not in href):
        raise CampaignValidationError("profile_identity_mismatch")
    return {"username": actual, "href": href}


async def open_comment_panel(page: Any, entry_definition: dict[str, Any], timeout_ms: int, *, resolver: Any | None = None) -> None:
    try:
        resolved = await _resolve(page, entry_definition, resolver=resolver)
        handle = getattr(resolved, "handle", resolved)
        await handle.click(timeout=timeout_ms)
    except CampaignValidationError:
        raise
    except Exception as exc:
        raise CampaignValidationError("comment_panel_not_ready") from exc


async def locate_comment_input(page: Any, input_definition: dict[str, Any], *, resolver: Any | None = None) -> Any:
    try:
        return await _resolve(page, input_definition, resolver=resolver, require_editable=True)
    except CampaignValidationError:
        raise
    except Exception as exc:
        raise CampaignValidationError("comment_input_not_found") from exc


async def locate_submit_control(page: Any, submit_definition: dict[str, Any], *, resolver: Any | None = None) -> Any:
    try:
        return await _resolve(page, submit_definition, resolver=resolver)
    except CampaignValidationError:
        raise
    except Exception as exc:
        raise CampaignValidationError("comment_panel_not_ready") from exc


async def _resolve(page: Any, definition: dict[str, Any], *, resolver: Any | None, require_editable: bool = False) -> Any:
    selected = resolver or StrictLocatorResolver()
    try:
        return await selected.resolve(page, definition, require_editable=require_editable)
    except LocatorResolutionError as exc:
        raise CampaignValidationError("comment_input_not_found" if require_editable else "comment_panel_not_ready") from exc
