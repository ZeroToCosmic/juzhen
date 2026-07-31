"""Timezone-aware scheduling rules for TikTok statistics collection."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def due_incremental_slots(
    now_utc: datetime,
    last_slot: datetime | None,
    timezone: str | ZoneInfo,
) -> list[datetime]:
    """Return each unprocessed three-hour local slot through ``now_utc``."""
    now = _aware_utc(now_utc, "now_utc")
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    if not isinstance(zone, ZoneInfo):
        raise TypeError("timezone must be a ZoneInfo or timezone name")

    local_now = now.astimezone(zone)
    if last_slot is None:
        cursor_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        last = _aware_utc(last_slot, "last_slot")
        cursor_local = last.astimezone(zone).replace(minute=0, second=0, microsecond=0)
        cursor_local += timedelta(hours=3 - (cursor_local.hour % 3))

    slots: list[datetime] = []
    while cursor_local <= local_now:
        slots.append(cursor_local.astimezone(UTC))
        cursor_local += timedelta(hours=3)
    return slots


def full_calibration_due(account_id: int, business_date: str | date, store) -> bool:
    """Return whether an account lacks a retained complete full daily snapshot."""
    date_text = business_date.isoformat() if isinstance(business_date, date) else business_date
    row = store.connection.execute(
        """
        SELECT 1
        FROM daily_account_metrics AS daily
        JOIN account_snapshots AS snapshot ON snapshot.id = daily.snapshot_id
        WHERE daily.account_id = ?
          AND daily.business_date = ?
          AND daily.baseline_status <> 'incomplete'
          AND snapshot.snapshot_type = 'full'
          AND snapshot.coverage = 'full'
        LIMIT 1
        """,
        (int(account_id), date_text),
    ).fetchone()
    return row is None


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)

