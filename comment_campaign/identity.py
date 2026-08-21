"""Immutable, safe TikTok account identity observations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from unicodedata import normalize

from .errors import CampaignValidationError


_ACCOUNT_KEY = re.compile(r"[a-z0-9._]{1,24}")


@dataclass(frozen=True, slots=True)
class AccountObservation:
    account_key: str
    visible_username: str
    canonical_href: str | None
    observed_at: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "account_key": self.account_key,
            "visible_username": self.visible_username,
            "canonical_href": self.canonical_href,
            "observed_at": self.observed_at,
        }


def normalize_tiktok_account_key(value: str) -> str:
    """Return the one safe TikTok account key, or fail closed."""
    normalized = normalize("NFKC", str(value)).strip()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    normalized = normalized.casefold()
    if _ACCOUNT_KEY.fullmatch(normalized) is None:
        raise CampaignValidationError("tiktok_identity_unavailable")
    return normalized


__all__ = ["AccountObservation", "normalize_tiktok_account_key"]
