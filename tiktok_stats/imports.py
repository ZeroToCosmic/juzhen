"""Safe TikTok account import and existing-account projection helpers."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from .store import StatsStore


_USERNAME_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9._]{0,22}[A-Za-z0-9_])?")
_TEXT_DELIMITER_RE = re.compile(r"[,\t\r\n]+")


@dataclass(frozen=True)
class NormalizedUsername:
    display_name: str
    username_key: str


@dataclass(frozen=True)
class ImportItemResult:
    value: object
    status: str
    account: dict | None = None
    error: str | None = None


@dataclass(frozen=True)
class ImportResult:
    results: list[ImportItemResult]

    @property
    def items(self) -> list[ImportItemResult]:
        return self.results

    @property
    def added(self) -> int:
        return self._count("added")

    @property
    def existing(self) -> int:
        return self._count("existing")

    @property
    def reactivated(self) -> int:
        return self._count("reactivated")

    @property
    def invalid(self) -> int:
        return self._count("invalid")

    def _count(self, status: str) -> int:
        return sum(item.status == status for item in self.results)


def normalize_tiktok_username(value: object) -> tuple[str, str]:
    """Return a safe display username and case-insensitive uniqueness key."""
    if not isinstance(value, str):
        raise ValueError("TikTok username must be text")
    raw = value.strip()
    if not raw:
        raise ValueError("TikTok username is required")

    username = _username_from_value(raw)
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("Invalid TikTok username")
    return username, username.casefold()


def parse_username_text(text: str) -> list[NormalizedUsername]:
    """Parse newline, comma, or tab-separated TikTok usernames."""
    if not isinstance(text, str):
        raise ValueError("TikTok username text must be text")
    return [
        NormalizedUsername(*normalize_tiktok_username(value))
        for value in _split_username_text(text)
    ]


def existing_account_candidates(accounts_db_path: str | Path, query: str | None = None) -> list[dict]:
    """Project TikTok channels from the legacy account DB without opening it for writing."""
    path = Path(accounts_db_path)
    database_uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection, connection:
        rows = connection.execute(
            """
            SELECT id, account_name, buffer_channels
            FROM accounts
            ORDER BY id ASC
            """
        ).fetchall()

    query_key = (query or "").strip().casefold()
    candidates: list[dict] = []
    seen_candidates: set[tuple[str, str]] = set()
    for account_id, account_name, raw_channels in rows:
        for channel in _tiktok_channels(raw_channels):
            descriptor = channel.get("descriptor")
            try:
                username, username_key = normalize_tiktok_username(descriptor)
            except ValueError:
                continue
            candidate = {
                "source_account_id": str(account_id),
                "account_name": str(account_name or ""),
                "channel_id": str(channel.get("id") or ""),
                "channel_name": str(channel.get("displayName") or ""),
                "username": username,
                "username_key": username_key,
            }
            if query_key and query_key not in _candidate_search_text(candidate):
                continue
            candidate_key = (candidate["source_account_id"], username_key)
            if candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)
            candidates.append(candidate)
    return candidates


def import_tracked_accounts(
    store: StatsStore,
    values: Iterable[object] | str,
    source: str,
    source_ids: Sequence[object | None] | None = None,
) -> ImportResult:
    """Import each supplied account independently, preserving valid partial results."""
    raw_values = _coerce_import_values(values)
    if source_ids is not None and len(source_ids) != len(raw_values):
        raise ValueError("source_ids must align with values")

    results: list[ImportItemResult] = []
    for index, value in enumerate(raw_values):
        source_id = None if source_ids is None else source_ids[index]
        try:
            display_name, username_key = _normalized_parts(value)
        except ValueError as error:
            results.append(ImportItemResult(value, "invalid", error=str(error)))
            continue

        account = store.tracked_account(username_key)
        if account is None:
            try:
                account = store.add_tracked_account(
                    display_name,
                    username_key,
                    source=source,
                    source_account_id=None if source_id is None else str(source_id),
                )
            except sqlite3.IntegrityError:
                account = store.tracked_account(username_key)
                if account is None:
                    raise
            else:
                results.append(ImportItemResult(value, "added", account))
                continue

        if account["status"] == "disabled":
            store.enable_account(account["id"])
            account = store.tracked_account(username_key)
            results.append(ImportItemResult(value, "reactivated", account))
        else:
            results.append(ImportItemResult(value, "existing", account))
    return ImportResult(results)


def _username_from_value(raw: str) -> str:
    if raw.startswith(("http://", "https://")):
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.tiktok.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("TikTok profile URL must be an exact https://www.tiktok.com/@username URL")
        path = parsed.path
        if not path.startswith("/@") or path.count("/") != 1:
            raise ValueError("TikTok URL must point to a profile")
        return path[2:]
    if "://" in raw:
        raise ValueError("TikTok profile URL must use HTTPS")
    return raw[1:] if raw.startswith("@") else raw


def _split_username_text(text: str) -> list[str]:
    return [value.strip() for value in _TEXT_DELIMITER_RE.split(text) if value.strip()]


def _coerce_import_values(values: Iterable[object] | str) -> list[object]:
    if isinstance(values, str):
        return list(_split_username_text(values))
    return list(values)


def _normalized_parts(value: object) -> tuple[str, str]:
    if isinstance(value, NormalizedUsername):
        return normalize_tiktok_username(value.display_name)
    return normalize_tiktok_username(value)


def _tiktok_channels(raw_channels: object) -> list[dict]:
    try:
        channels = json.loads(raw_channels or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(channels, list):
        return []
    return [
        channel
        for channel in channels
        if isinstance(channel, dict) and _is_tiktok_channel(channel)
    ]


def _is_tiktok_channel(channel: dict) -> bool:
    # Mirrors gateway.buffer_discovery.is_tiktok_channel's service/descriptor semantics
    # while retaining this import boundary's dict-only input check.
    service = str(channel.get("service") or "").casefold()
    descriptor = str(channel.get("descriptor") or "").casefold()
    return "tiktok" in service or "tiktok" in descriptor


def _candidate_search_text(candidate: dict) -> str:
    return " ".join(
        str(candidate[field]).casefold()
        for field in ("account_name", "channel_id", "channel_name", "username", "username_key")
    )
