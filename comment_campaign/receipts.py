"""Receipt evidence is deterministic, redacted, and never a retry signal."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import PurePosixPath
from unicodedata import normalize
from uuid import uuid4
from typing import Any

from .errors import CampaignValidationError


def normalize_comment_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(normalize("NFKC", value).replace("\u00a0", " ").split())


def text_sha256(value: str) -> str:
    return sha256(normalize_comment_text(value).encode("utf-8")).hexdigest()


def evidence_filename() -> str:
    return f"{uuid4().hex}.png"


def safe_evidence_path(value: str) -> str:
    if "\\" in str(value) or "%" in str(value):
        raise CampaignValidationError("comment_receipt_unverified")
    path = PurePosixPath(str(value).replace("\\", "/"))
    if (path.suffix != ".png" or len(path.parts) != 2 or path.parts[0] != "evidence"
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.is_absolute() or ":" in str(path)):
        raise CampaignValidationError("comment_receipt_unverified")
    if len(path.stem) != 32 or any(char not in "0123456789abcdef" for char in path.stem):
        raise CampaignValidationError("comment_receipt_unverified")
    return path.as_posix()


def build_receipt(*, video_id: str, profile_ref: str, expected_username: str, text: str, screenshot_path: str | None, status: str = "pending", **evidence: Any) -> dict[str, Any]:
    normalized = normalize_comment_text(text)
    if not normalized:
        raise CampaignValidationError("comment_receipt_unverified")
    return {
        "receipt_id": f"receipt_{uuid4().hex}", "status": status,
        "video_id": str(video_id), "profile_ref": str(profile_ref),
        "expected_username": str(expected_username), "normalized_text": normalized,
        "normalized_text_hash": text_sha256(normalized),
        "screenshot_path": safe_evidence_path(screenshot_path) if screenshot_path else None,
        "posting_window_started_at": datetime.now(timezone.utc).isoformat(),
        **{key: value for key, value in evidence.items() if key in {"author_profile_href", "platform_comment_id", "comment_permalink", "locator_candidates", "stable_attributes", "parent_receipt_id", "parent_platform_comment_id", "parent_comment_permalink", "parent_stable_attributes", "parent_author"}},
    }


async def verify_receipt(page: Any, receipt: dict[str, Any], comment_definition: dict[str, Any], *, resolver: Any) -> bool:
    """A receipt is verified only when one exact visible comment node remains."""
    try:
        resolved = await resolver.resolve(page, comment_definition)
        handle = getattr(resolved, "handle", resolved)
        evidence = await handle.evaluate("""element => ({
            text: String(element.innerText || element.textContent || ''),
            href: String(element.querySelector('a[href]')?.href || ''),
            id: String(element.getAttribute('data-e2e') || element.id || '')
        })""")
    except Exception:
        return False
    return (isinstance(evidence, dict)
            and text_sha256(str(evidence.get("text", ""))) == receipt.get("normalized_text_hash")
            and (not receipt.get("author_profile_href") or evidence.get("href") == receipt.get("author_profile_href"))
            and (not receipt.get("platform_comment_id") or evidence.get("id") == receipt.get("platform_comment_id")))


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    stable = candidate.get("stable_attributes")
    if not isinstance(stable, dict):
        stable = {}
    value = {
        "id": candidate.get("platform_comment_id", ""),
        "permalink": candidate.get("comment_permalink", ""),
        "stable": stable,
    }
    encoded = repr(sorted(value.items())).encode("utf-8")
    return sha256(encoded).hexdigest()


def verify_receipt_candidates(*, before: list[dict[str, Any]], after: list[dict[str, Any]], receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Accept exactly one new visible candidate matching immutable receipt facts."""
    old = {candidate_fingerprint(item) for item in before if isinstance(item, dict)}
    matches: list[dict[str, Any]] = []
    try:
        window_start = datetime.fromisoformat(str(receipt.get("posting_window_started_at")))
    except (TypeError, ValueError):
        return None
    for item in after:
        if not isinstance(item, dict) or item.get("visible") is not True or candidate_fingerprint(item) in old:
            continue
        candidate_profile = str(item.get("profile_ref") or "").strip()
        if candidate_profile:
            identity_matches = candidate_profile == str(receipt.get("profile_ref") or "")
        else:
            author = str(item.get("author_username") or "").lstrip("@").casefold()
            expected = str(receipt.get("expected_username") or "").lstrip("@").casefold()
            identity_matches = bool(author and expected and author == expected)
        stable = item.get("stable_attributes")
        try:
            observed_at = datetime.fromisoformat(str(item.get("observed_at")))
        except (TypeError, ValueError):
            continue
        has_stable_node = bool(
            item.get("platform_comment_id")
            or item.get("comment_permalink")
            or (isinstance(stable, dict) and stable)
        )
        parent_scope_matches = True
        if receipt.get("parent_receipt_id"):
            expected_parent_id = str(receipt.get("parent_platform_comment_id") or "")
            expected_parent_link = str(receipt.get("parent_comment_permalink") or "")
            expected_parent_stable = receipt.get("parent_stable_attributes")
            has_expected_scope = bool(expected_parent_id or expected_parent_link or (isinstance(expected_parent_stable, dict) and expected_parent_stable))
            parent_scope_matches = has_expected_scope and (
                (bool(expected_parent_id) and str(item.get("parent_platform_comment_id") or "") == expected_parent_id)
                or (bool(expected_parent_link) and str(item.get("parent_comment_permalink") or "") == expected_parent_link)
                or (isinstance(expected_parent_stable, dict) and bool(expected_parent_stable)
                    and isinstance(item.get("parent_stable_attributes"), dict)
                    and all(item["parent_stable_attributes"].get(key) == value for key, value in expected_parent_stable.items()))
            )
        if (str(item.get("video_id") or "") != str(receipt.get("video_id") or "")
                or not identity_matches
                or text_sha256(str(item.get("text") or "")) != receipt.get("normalized_text_hash")
                or not has_stable_node
                or observed_at < window_start
                or not parent_scope_matches):
            continue
        matches.append(item)
    if len(matches) != 1:
        return None
    return matches[0]


async def collect_comment_candidates(page: Any, campaign: dict[str, Any], _assignment: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect only visible comment nodes carrying a unique DOM identifier."""
    try:
        value = await page.evaluate(
            """videoId => Array.from(document.querySelectorAll('[data-comment-id], [id*="comment"]')).map(node => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                const visible = style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                const commentId = String(node.getAttribute('data-comment-id') || node.id || '');
                const parentNode = node.parentElement?.closest('[data-comment-id], [id*="comment"]');
                const parentCommentId = String(parentNode?.getAttribute('data-comment-id') || parentNode?.id || '');
                const parentPermalink = String(parentNode?.querySelector('a[href*="/comment/"]')?.href || '');
                const authorLink = node.querySelector('a[href*="/@"]');
                const href = String(authorLink?.href || '');
                const match = href.match(/@([^/?]+)/);
                const textNode = node.querySelector('[data-e2e*="comment-text"], p, [dir="auto"]');
                const text = String(textNode?.innerText || textNode?.textContent || '');
                const permalink = String(node.querySelector('a[href*="/comment/"]')?.href || '');
                return {
                    video_id: String(videoId), visible, text,
                    author_username: match ? decodeURIComponent(match[1]) : '',
                    author_profile_href: href, platform_comment_id: commentId,
                    parent_platform_comment_id: parentCommentId,
                    parent_comment_permalink: parentPermalink,
                    parent_stable_attributes: parentCommentId ? {comment_id: parentCommentId} : {},
                    comment_permalink: permalink,
                    stable_attributes: commentId ? {comment_id: commentId} : {}
                };
            }).filter(item => item.visible && item.platform_comment_id)""",
            str(campaign.get("video_id") or ""),
        )
    except Exception:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return []
    observed_at = datetime.now(timezone.utc).isoformat()
    return [{**dict(item), "observed_at": observed_at} for item in value]
