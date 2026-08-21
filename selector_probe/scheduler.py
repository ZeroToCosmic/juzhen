from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be timezone-aware") from error
    if offset is None:
        raise ValueError(f"{name} must be timezone-aware")


def _valid_local_instants(
    local_value: datetime,
    zone: ZoneInfo,
) -> tuple[datetime, ...]:
    instants: set[datetime] = set()
    for fold in (0, 1):
        candidate = local_value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        round_trip = candidate.astimezone(zone).replace(tzinfo=None)
        if round_trip == local_value:
            instants.add(candidate)
    return tuple(sorted(instants))


def _scheduled_instant(
    candidate_date: date,
    daily_time: time,
    zone: ZoneInfo,
) -> datetime:
    local_value = datetime.combine(
        candidate_date,
        daily_time.replace(tzinfo=None),
    )
    instants = _valid_local_instants(local_value, zone)
    if instants:
        # A repeated wall time is one daily slot: use its first occurrence.
        return instants[0]

    # A skipped wall time is normalized to the first valid local minute after
    # the gap. Probe configuration has minute precision (HH:MM).
    cursor = local_value.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(3 * 60):
        instants = _valid_local_instants(cursor, zone)
        if instants:
            return instants[0]
        cursor += timedelta(minutes=1)
    raise ValueError("could not resolve daily_time in configured timezone")


def due_daily_slot(
    now_utc: datetime,
    last_completed_slot: datetime | None,
    timezone: str,
    daily_time: time,
) -> datetime | None:
    """Return the newest due daily slot, expressed in UTC."""

    _require_aware(now_utc, "now_utc")
    if last_completed_slot is not None:
        _require_aware(last_completed_slot, "last_completed_slot")
    if not isinstance(daily_time, time):
        raise ValueError("daily_time must be a datetime.time")

    try:
        zone = ZoneInfo(timezone)
    except (TypeError, ZoneInfoNotFoundError) as error:
        raise ValueError(f"invalid timezone: {timezone!r}") from error

    now = now_utc.astimezone(UTC)
    local_date = now.astimezone(zone).date()
    candidate = _scheduled_instant(local_date, daily_time, zone)
    if candidate > now:
        candidate = _scheduled_instant(
            local_date - timedelta(days=1),
            daily_time,
            zone,
        )
    if candidate > now:
        return None
    if (
        last_completed_slot is not None
        and last_completed_slot.astimezone(UTC) >= candidate
    ):
        return None
    return candidate


class RedisLease:
    def __init__(
        self,
        client: object,
        key: str,
        owner_id: str,
        ttl_seconds: int = 120,
        heartbeat_seconds: int = 30,
    ) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a non-empty string")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must be a non-empty string")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if type(heartbeat_seconds) is not int or heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be a positive integer")
        if heartbeat_seconds >= ttl_seconds:
            raise ValueError("heartbeat_seconds must be less than ttl_seconds")
        self.client = client
        self.key = key
        self.owner_id = owner_id
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def acquire(self) -> bool:
        return bool(
            self.client.set(
                self.key,
                self.owner_id,
                nx=True,
                ex=self.ttl_seconds,
            )
        )

    def renew(self) -> bool:
        return bool(
            self.client.eval(
                RENEW_SCRIPT,
                1,
                self.key,
                self.owner_id,
                self.ttl_seconds,
            )
        )

    def release(self) -> bool:
        return bool(
            self.client.eval(
                RELEASE_SCRIPT,
                1,
                self.key,
                self.owner_id,
            )
        )
