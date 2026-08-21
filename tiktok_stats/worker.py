"""Durable scheduler worker for local TikTok statistics collection."""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from .client import TikTokApiClient
from .collector import AccountCollectionResult, Collector
from .scheduler import due_incremental_slots, full_calibration_due
from .secrets import CookieSecretStore
from .store import StatsStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class WorkerTickResult:
    incremental_run_ids: tuple[int, ...] = ()
    full_run_id: int | None = None
    cleanup_run_id: int | None = None
    skipped_leases: tuple[str, ...] = ()


class SystemClock:
    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)


class LeaseHeartbeat:
    """Renew a lease on a dedicated SQLite connection until explicitly stopped."""

    def __init__(
        self,
        *,
        store_path: Path,
        lease_name: str,
        owner_id: str,
        clock,
        lease_seconds: int,
        interval_seconds: float | None = None,
        on_renew: Callable[[], object] | None = None,
    ):
        self.store_path = Path(store_path)
        self.lease_name = lease_name
        self.owner_id = owner_id
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.interval_seconds = (
            float(interval_seconds)
            if interval_seconds is not None
            else max(0.1, lease_seconds / 3)
        )
        self.on_renew = on_renew
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def start(self) -> "LeaseHeartbeat":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name=f"stats-lease-{self.lease_name}",
            daemon=True,
        )
        self._thread.start()
        return self

    def pulse(self) -> None:
        """Request an immediate renewal; useful for deterministic synchronization."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("worker lease heartbeat failed") from self._failure

    def _run(self) -> None:
        try:
            with StatsStore(self.store_path) as heartbeat_store:
                while True:
                    self._wake.wait(self.interval_seconds)
                    self._wake.clear()
                    if self._stop.is_set():
                        return
                    now = _clock_now(self.clock)
                    renewed = heartbeat_store.renew_lease(
                        self.lease_name,
                        self.owner_id,
                        _utc_iso(now + timedelta(seconds=self.lease_seconds)),
                        now=_utc_iso(now),
                    )
                    if not renewed:
                        raise RuntimeError("worker lease ownership was lost")
                    if self.on_renew is not None:
                        self.on_renew()
        except BaseException as error:
            self._failure = error


def run_worker_once(
    store: StatsStore,
    collector: Collector,
    *,
    clock,
    sleeper: Callable[[float], object],
    rng,
    owner_id: str,
    timezone: str | ZoneInfo = SHANGHAI,
    lease_seconds: int = 300,
    account_jitter_seconds: float = 15,
    include_incremental: bool = True,
    include_full: bool = True,
    heartbeat_factory=LeaseHeartbeat,
) -> WorkerTickResult:
    """Claim and execute jobs due at the injected clock instant."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if account_jitter_seconds < 0:
        raise ValueError("account_jitter_seconds must be non-negative")
    now = _clock_now(clock)
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    last_slot = store.last_scheduled_slot("incremental")
    due_slots = due_incremental_slots(now, last_slot, zone) if include_incremental else []
    if last_slot is None and due_slots:
        due_slots = due_slots[-1:]
    if include_incremental:
        pending_slots = store.running_scheduled_slots("incremental", _utc_iso(now))
        due_slots = sorted(set(due_slots).union(pending_slots))

    incremental_ids: list[int] = []
    skipped: list[str] = []
    for slot in due_slots:
        scheduled_for = _utc_iso(slot)
        lease_name = f"incremental:{scheduled_for}"
        run_id = _run_collection_job(
            store,
            collector,
            run_type="incremental",
            scheduled_for=scheduled_for,
            lease_name=lease_name,
            owner_id=owner_id,
            clock=clock,
            sleeper=sleeper,
            rng=rng,
            lease_seconds=lease_seconds,
            account_jitter_seconds=account_jitter_seconds,
            business_date=None,
            heartbeat_factory=heartbeat_factory,
        )
        if run_id is None:
            skipped.append(lease_name)
        else:
            incremental_ids.append(run_id)

    full_run_id = None
    if include_full:
        business_date = now.astimezone(zone).date().isoformat()
        due_accounts = [
            int(account["id"])
            for account in store.enabled_accounts()
            if full_calibration_due(int(account["id"]), business_date, store)
        ]
        running_full_run_id = store.running_full_run_id(business_date)
        if due_accounts or running_full_run_id is not None:
            scheduled_for = business_date
            lease_name = f"full:{business_date}"
            full_run_id = _run_collection_job(
                store,
                collector,
                run_type="full",
                scheduled_for=scheduled_for,
                lease_name=lease_name,
                owner_id=owner_id,
                clock=clock,
                sleeper=sleeper,
                rng=rng,
                lease_seconds=lease_seconds,
                account_jitter_seconds=account_jitter_seconds,
                business_date=business_date,
                account_ids=due_accounts,
                heartbeat_factory=heartbeat_factory,
            )
            if full_run_id is None:
                skipped.append(lease_name)

    return WorkerTickResult(
        incremental_run_ids=tuple(incremental_ids),
        full_run_id=full_run_id,
        skipped_leases=tuple(skipped),
    )


def run_cleanup(store: StatsStore, *, clock, retention_days: int = 90) -> WorkerTickResult:
    now = _clock_now(clock)
    cutoff = _utc_iso(now - timedelta(days=retention_days))
    run_id = store.start_run("cleanup", scheduled_for=_utc_iso(now))
    try:
        deleted = store.cleanup_snapshots(cutoff)
    except Exception:
        store.finish_run(run_id, "failed", details_json='{"error":"cleanup_failed"}')
        raise
    store.finish_run(
        run_id,
        "completed",
        details_json=json.dumps({"deleted_snapshots": deleted}, separators=(",", ":")),
    )
    return WorkerTickResult(cleanup_run_id=run_id)


def _run_collection_job(
    store,
    collector,
    *,
    run_type: str,
    scheduled_for: str,
    lease_name: str,
    owner_id: str,
    clock,
    sleeper,
    rng,
    lease_seconds: int,
    account_jitter_seconds: float,
    business_date: str | None,
    account_ids: Sequence[int] | None = None,
    heartbeat_factory=LeaseHeartbeat,
) -> int | None:
    now = _clock_now(clock)
    if not store.acquire_lease(
        lease_name,
        owner_id,
        _utc_iso(now + timedelta(seconds=lease_seconds)),
        now=_utc_iso(now),
    ):
        return None
    run_id = None
    try:
        run_id = (
            store.claim_full_run(business_date, allow_new=bool(account_ids))
            if run_type == "full" and business_date is not None
            else store.claim_scheduled_run(run_type, scheduled_for)
        )
        if run_id is None:
            return None
        ids = list(account_ids) if account_ids is not None else [
            int(account["id"]) for account in store.enabled_accounts()
        ]
        results: list[AccountCollectionResult] = []
        heartbeat = heartbeat_factory(
            store_path=store.path,
            lease_name=lease_name,
            owner_id=owner_id,
            clock=clock,
            lease_seconds=lease_seconds,
        ).start()
        try:
            for account_id in ids:
                if account_jitter_seconds:
                    sleeper(float(rng.random()) * account_jitter_seconds)
                _renew_or_raise(store, lease_name, owner_id, clock, lease_seconds)
                with store.lease_write_guard(lease_name, owner_id, clock):
                    if run_type == "full":
                        result = collector.collect_full(account_id, run_id, business_date)
                    else:
                        result = collector.collect_incremental(account_id, run_id)
                results.append(result)
                heartbeat.raise_if_failed()
                _renew_or_raise(store, lease_name, owner_id, clock, lease_seconds)
        finally:
            heartbeat.stop()
        heartbeat.raise_if_failed()
        now = _clock_now(clock)
        if not store.finish_run_if_lease_owner(
            run_id,
            _collection_status(results),
            lease_name=lease_name,
            owner_id=owner_id,
            now=_utc_iso(now),
            details_json=_run_details(results),
        ):
            raise RuntimeError("worker lease ownership was lost before run completion")
        return run_id
    except Exception:
        if run_id is not None:
            now = _clock_now(clock)
            store.finish_run_if_lease_owner(
                run_id,
                "failed",
                lease_name=lease_name,
                owner_id=owner_id,
                now=_utc_iso(now),
                details_json='{"error":"worker_failed"}',
            )
        raise
    finally:
        store.release_lease(lease_name, owner_id)


def _renew_or_raise(store, lease_name: str, owner_id: str, clock, lease_seconds: int) -> None:
    now = _clock_now(clock)
    if not store.renew_lease(
        lease_name,
        owner_id,
        _utc_iso(now + timedelta(seconds=lease_seconds)),
        now=_utc_iso(now),
    ):
        raise RuntimeError("worker lease ownership was lost")


def _collection_status(results: Sequence[AccountCollectionResult]) -> str:
    successes = sum(result.succeeded for result in results)
    if successes == len(results):
        return "completed"
    return "partial" if successes else "failed"


def _run_details(results: Sequence[AccountCollectionResult]) -> str:
    return json.dumps(
        {
            "account_count": len(results),
            "success_count": sum(result.succeeded for result in results),
            "failure_count": sum(not result.succeeded for result in results),
            "errors": [
                {"account_id": result.account_id, "code": result.error_code}
                for result in results
                if not result.succeeded
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _clock_now(clock) -> datetime:
    value = clock() if callable(clock) else clock.now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _current_slot(now: datetime, zone: ZoneInfo) -> datetime:
    local = now.astimezone(zone)
    return local.replace(
        hour=local.hour - local.hour % 3, minute=0, second=0, microsecond=0
    ).astimezone(UTC)


def _runtime_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return (
        Path(os.getenv("TIKTOK_STATS_DB_PATH", root / "data" / "stats" / "tiktok_stats.db")),
        Path(os.getenv("TIKTOK_STATS_COOKIE_PATH", root / "data" / "stats" / "tiktok_cookie.json")),
    )


def _build_runtime():
    db_path, cookie_path = _runtime_paths()
    store = StatsStore(db_path)
    secret_store = CookieSecretStore(cookie_path)
    client = TikTokApiClient(
        os.getenv("TIKTOK_STATS_API_URL", "http://127.0.0.1:53281"),
        secret_store.load_cookie,
    )
    clock = SystemClock()
    rng = random.Random()
    collector = Collector(
        store,
        client,
        clock,
        time.sleep,
        rng,
        cookie_status_callback=secret_store,
    )
    return store, client, collector, clock, rng, secret_store


def _validate_cookie(store, client, secret_store, clock) -> int:
    accounts = store.enabled_accounts()
    if not accounts or not secret_store.load_cookie():
        return 1
    try:
        account = accounts[0]
        sec_uid = account.get("sec_uid") or client.resolve_sec_uid(str(account["username"]))
        client.fetch_profile(str(sec_uid))
    except Exception:
        secret_store.mark_validation(False, "validation failed", clock.now())
        return 1
    secret_store.mark_validation(True, "validation succeeded", clock.now())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TikTok statistics worker")
    parser.add_argument(
        "command",
        choices=("serve", "tick", "incremental", "full", "cleanup", "validate-cookie"),
        nargs="?",
        default="serve",
    )
    args = parser.parse_args(argv)
    store, client, collector, clock, rng, secret_store = _build_runtime()
    owner_id = f"worker-{uuid.uuid4().hex}"
    try:
        if args.command == "validate-cookie":
            return _validate_cookie(store, client, secret_store, clock)
        if args.command == "cleanup":
            run_cleanup(store, clock=clock)
            return 0
        if args.command in {"tick", "incremental", "full"}:
            run_worker_once(
                store,
                collector,
                clock=clock,
                sleeper=time.sleep,
                rng=rng,
                owner_id=owner_id,
                include_incremental=args.command != "full",
                include_full=args.command != "incremental",
            )
            return 0

        stopping = False
        stop_file_value = os.getenv("TIKTOK_STATS_STOP_FILE", "").strip()
        stop_file = Path(stop_file_value) if stop_file_value else None

        def request_stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        last_cleanup_date = None
        while not stopping and not _stop_file_exists(stop_file):
            run_worker_once(
                store,
                collector,
                clock=clock,
                sleeper=time.sleep,
                rng=rng,
                owner_id=owner_id,
            )
            today = clock.now().astimezone(SHANGHAI).date()
            if last_cleanup_date != today:
                run_cleanup(store, clock=clock)
                last_cleanup_date = today
            if not stopping:
                for _ in range(60):
                    if stopping or _stop_file_exists(stop_file):
                        break
                    time.sleep(0.5)
        return 0
    finally:
        client.close()
        store.close()


def _stop_file_exists(path: Path | None) -> bool:
    return path is not None and path.exists()


if __name__ == "__main__":
    sys.exit(main())
