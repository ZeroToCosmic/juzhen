"""Public presentation helpers for internal browser identifiers."""

from __future__ import annotations

import re


_PUBLIC_PROFILE_ID = re.compile(r"^\*\*\*.{4}$", re.DOTALL)


def mask_profile_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _PUBLIC_PROFILE_ID.fullmatch(text):
        return text
    if len(text) < 4:
        return "***"
    return f"***{text[-4:]}"
