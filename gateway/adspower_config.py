"""Resolve the live AdsPower settings without retaining credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdsPowerConfig:
    base_url: str
    api_key: str


def resolve_adspower_config(settings_loader, environ=None) -> AdsPowerConfig | None:
    """Use non-empty persisted values, with environment fallback per field."""
    environment = os.environ if environ is None else environ
    settings = settings_loader()
    adspower = settings.get("adspower", {}) if isinstance(settings, Mapping) else {}
    source = adspower if isinstance(adspower, Mapping) else {}
    base_url = str(source.get("base_url") or "").strip()
    if not base_url:
        base_url = str(environment.get("ADSPOWER_BASE_URL") or "").strip()
    api_key = str(source.get("api_key") or "").strip()
    if not api_key:
        api_key = str(environment.get("ADSPOWER_API_KEY") or "").strip()
    return AdsPowerConfig(base_url, api_key) if base_url and api_key else None
