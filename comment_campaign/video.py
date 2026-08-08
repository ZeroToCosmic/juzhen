"""Strict normalization for TikTok video targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import CampaignValidationError


VIDEO_PATH = re.compile(r"^/@([A-Za-z0-9._]{1,24})/video/([0-9]{8,30})/?$")


@dataclass(frozen=True, slots=True)
class TargetVideo:
    video_id: str
    canonical_url: str


def normalize_tiktok_video(reference: str) -> TargetVideo:
    """Accept only a direct HTTPS TikTok video permalink and remove its query."""
    if not isinstance(reference, str) or not reference or len(reference) > 2_000:
        raise CampaignValidationError("target_video_invalid")
    try:
        parsed = urlparse(reference.strip())
        port = parsed.port
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except (ValueError, UnicodeError) as exc:
        raise CampaignValidationError("target_video_invalid") from exc
    if (
        parsed.scheme != "https"
        or hostname not in {"tiktok.com", "www.tiktok.com"}
        or username is not None
        or password is not None
        or port is not None
        or "%" in parsed.path
        or "\\" in parsed.path
        or any(ord(character) < 32 for character in parsed.path)
    ):
        raise CampaignValidationError("target_video_invalid")
    match = VIDEO_PATH.fullmatch(parsed.path)
    if match is None or parsed.params or parsed.fragment:
        raise CampaignValidationError("target_video_invalid")
    username, video_id = match.groups()
    return TargetVideo(video_id=video_id, canonical_url=f"https://www.tiktok.com/@{username}/video/{video_id}")
