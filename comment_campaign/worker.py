"""RQ worker process for Comment Campaign jobs, with no import-time RQ dependency."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .queueing import PREFIX, QUEUE_NAME, RedisLease, RedisUnavailableError
from .worker_identity import build_worker_health_value, project_fingerprint


WORKER_HEALTH_KEY = PREFIX + "worker:health"
WORKER_HEALTH_VALUE = "worker"
WORKER_HEALTH_TTL_SECONDS = 30
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _rq_worker_class():
    """Use RQ's spawn-based worker on Windows, where ``fork`` is unavailable."""
    try:
        from rq import Worker
        if os.name == "nt":
            from rq import SpawnWorker
            return SpawnWorker
        return Worker
    except ImportError as exc:
        raise RuntimeError(
            "Comment Campaign worker requires rq>=2.2"
        ) from exc


def build_runtime_service():
    """Build the complete worker runtime lazily; empty element IDs fail closed."""
    from adspower import AdsPowerController
    from execution_v2.adspower_adapter import RateLimitedAdsPowerAdapter
    from execution_v2.locator import StrictLocatorResolver
    from execution_v2.service import _OwnedPlaywrightSessions
    from execution_v2.store import ExecutionStore
    from execution_v2.tiling import tile_browser_bindings
    from .executor import CommentExecutor
    from .profile_gateway import ProfileGateway
    from .receipts import collect_comment_candidates
    from .service import create_default_comment_campaign_service
    from .adspower_health import AdsPowerHealthProbe
    from gateway.adspower_config import resolve_adspower_config
    from adspower import AdsPowerDependencyError

    coordinator = __import__("comment_campaign.queueing", fromlist=["QueueCoordinator"]).QueueCoordinator.from_url(
        os.getenv("COMMENT_CAMPAIGN_REDIS_URL", "redis://127.0.0.1:6379/0")
    )
    from gateway.settings_store import load_settings

    persisted = load_settings()
    campaign_settings = persisted.get("comment_campaign", {}) if isinstance(persisted, dict) else {}

    def health_settings():
        return resolve_adspower_config(load_settings, os.environ)

    config = resolve_adspower_config(lambda: persisted, os.environ)
    if config is None:
        raise AdsPowerDependencyError("not_configured")
    controller = AdsPowerController(base_url=config.base_url, api_key=config.api_key)
    def discovery():
        rows = controller.list_all_profiles()
        return [{"id": str(row.get("id") or ""), "name": str(row.get("name") or ""), "status": str(row.get("status") or "")} for row in rows if isinstance(row, dict)]
    health_probe = AdsPowerHealthProbe(
        AdsPowerController, health_settings, timeout_seconds=4.0
    )
    def adspower_probe():
        return health_probe.probe()
    campaign_service = create_default_comment_campaign_service(
        database_url=os.getenv("COMMENT_CAMPAIGN_DB_URL", "sqlite:///data/comment_campaign/comment_campaign.db"),
        queue_coordinator=coordinator,
        profile_provider=discovery,
        adspower_probe=adspower_probe,
        runtime_closeables=(health_probe,),
    )
    execution_store = ExecutionStore(os.getenv("EXECUTION_V2_DB_PATH", "data/execution_v2/execution_v2.db"))
    execution_store.initialize()
    def playwright_factory():
        from playwright.async_api import async_playwright
        return async_playwright()
    from secrets import token_urlsafe
    from .queueing import RedisLease
    def lease_factory(key: str):
        return RedisLease(coordinator.redis, key, token_urlsafe(24), ttl_seconds=120)
    bindings = campaign_settings.get("element_bindings", {}) if isinstance(campaign_settings, dict) else {}
    ids = {
        "entry_element_id": os.getenv("COMMENT_CAMPAIGN_ENTRY_ELEMENT_ID", bindings.get("entry_element_id", "")),
        "input_element_id": os.getenv("COMMENT_CAMPAIGN_INPUT_ELEMENT_ID", bindings.get("input_element_id", "")),
        "submit_element_id": os.getenv("COMMENT_CAMPAIGN_SUBMIT_ELEMENT_ID", bindings.get("submit_element_id", "")),
        "account_element_id": os.getenv("COMMENT_CAMPAIGN_ACCOUNT_ELEMENT_ID", bindings.get("account_element_id", "")),
    }
    gateway = ProfileGateway(campaign_service.store, RateLimitedAdsPowerAdapter(controller), _OwnedPlaywrightSessions(playwright_factory), profile_discovery=discovery, tiler=tile_browser_bindings, lease_factory=lease_factory)
    campaign_service._executor = CommentExecutor(campaign_service.store, gateway, StrictLocatorResolver(), element_provider=execution_store.get_element, settings_provider=lambda _campaign: ids, lease_factory=lease_factory, receipt_candidate_provider=collect_comment_candidates, evidence_dir=os.getenv("COMMENT_CAMPAIGN_EVIDENCE_DIR", "data/comment_campaign/evidence"), queue_coordinator=coordinator)
    campaign_service._runtime_closeables.append(execution_store)
    return campaign_service


def serve(
    *,
    store_factory: Callable[[], Any] | None = None,
    redis_factory: Callable[[str], Any] | None = None,
    queue_factory: Callable[[Any], Any] | None = None,
    worker_factory: Callable[[Any, Any], Any] | None = None,
    heartbeat_interval_seconds: float = 10,
    expected_project_fingerprint: str | None = None,
    owner_nonce: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Recover uncertain submissions, advertise health, then run RQ scheduler."""
    current_fingerprint = project_fingerprint(PROJECT_ROOT)
    if (
        expected_project_fingerprint is not None
        and expected_project_fingerprint != current_fingerprint
    ):
        raise RuntimeError("Comment Campaign worker project fingerprint mismatch")
    health_value = build_worker_health_value(
        os.getpid(), PROJECT_ROOT, owner_nonce or uuid.uuid4().hex
    )
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass
    if store_factory is not None:
        store = store_factory()
    else:
        # The long-lived worker only needs recovery state. Each RQ job owns and
        # closes its browser runtime on that job's event loop.
        from .store import CampaignStore

        store = CampaignStore(
            os.getenv(
                "COMMENT_CAMPAIGN_DB_URL",
                "sqlite:///data/comment_campaign/comment_campaign.db",
            )
        )
        store.initialize()
    stop = threading.Event()
    try:
        if redis_factory is None or queue_factory is None or worker_factory is None:
            try:
                import redis
                from rq import Queue
            except ImportError as exc:
                raise RuntimeError("Comment Campaign worker requires redis and rq") from exc
            worker_class = _rq_worker_class()
            redis_factory = redis.Redis.from_url if redis_factory is None else redis_factory
            queue_factory = (lambda connection: Queue(QUEUE_NAME, connection=connection)) if queue_factory is None else queue_factory
            worker_factory = (lambda queue, connection: worker_class([queue], connection=connection)) if worker_factory is None else worker_factory
        redis_url = os.getenv("COMMENT_CAMPAIGN_REDIS_URL", "redis://127.0.0.1:6379/0")
        connection = redis_factory(redis_url)

        health_lease = RedisLease(
            connection,
            "worker:health",
            health_value,
            ttl_seconds=WORKER_HEALTH_TTL_SECONDS,
        )
        if not health_lease.acquire():
            raise RuntimeError("Comment Campaign worker health lease is already held")
        def heartbeat() -> None:
            while not stop.is_set():
                try:
                    if not health_lease.refresh():
                        return
                except RedisUnavailableError:
                    return
                if heartbeat_interval_seconds <= 0:
                    return
                stop.wait(heartbeat_interval_seconds)

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        # Start heartbeat before SQLite recovery/pagination: recovery can take
        # longer than the lease TTL, but must never lose worker ownership.
        recover_campaigns = getattr(store, "recover_interrupted_campaigns", None)
        recovered = recover_campaigns() if callable(recover_campaigns) else None
        if recovered is None:
            store.recover_interrupted_submissions()
        if isinstance(recovered, dict):
            from .queueing import QueueCoordinator
            recovery_queue = QueueCoordinator(queue_factory(connection), redis=connection)
            clock = now or (lambda: datetime.now(timezone.utc))
            accepted_campaigns: set[str] = set()
            get_campaign = getattr(store, "get_campaign", None)
            preflight_required = getattr(store, "account_preflight_required", None)

            def current_campaign(campaign_id: str, fallback: Any = None) -> dict | None:
                current = get_campaign(campaign_id) if callable(get_campaign) else fallback
                return current if isinstance(current, dict) else None

            def needs_prepare(campaign: dict) -> bool:
                campaign_id = campaign["id"]
                if callable(preflight_required) and preflight_required(campaign_id):
                    return True
                eligible_ids = getattr(store, "eligible_assignment_ids", None)
                return bool(eligible_ids(campaign_id)) if callable(eligible_ids) else False

            def future_schedule_at(campaign: dict) -> datetime | None:
                if campaign.get("start_mode") != "scheduled":
                    return None
                scheduled_at = campaign.get("scheduled_at")
                if not isinstance(scheduled_at, str):
                    return None
                try:
                    candidate = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if candidate.tzinfo is None:
                    return None
                current = clock()
                if current.tzinfo is None:
                    return None
                candidate = candidate.astimezone(timezone.utc)
                return candidate if candidate > current.astimezone(timezone.utc) else None

            def enqueue_prepare(campaign_id: str, generation: int, campaign: dict) -> Any:
                identity_generation = campaign["identity_generation"]
                scheduled_at = future_schedule_at(campaign)
                if scheduled_at is not None:
                    return recovery_queue.enqueue_at(
                        campaign_id, scheduled_at, generation, identity_generation
                    )
                return recovery_queue.enqueue_prepare_generation(
                    campaign_id, generation, identity_generation
                )

            for campaign_id, result in recovered.items():
                generation = result.get("prepare_generation") if isinstance(result, dict) else None
                campaign = current_campaign(campaign_id)
                if (
                    type(generation) is int and generation > 0
                    and campaign is not None
                    and campaign.get("status") in {"queued", "running"}
                    and type(campaign.get("identity_generation")) is int
                    and needs_prepare(campaign)
                ):
                    enqueue_prepare(campaign_id, generation, campaign)
                    store.mark_reconcile_prepare_generation(campaign_id, generation)
                    accepted_campaigns.add(campaign_id)
            # Restart also repairs a lost Redis job for otherwise normal queued
            # work.  Scan every page, never submit, and retain the durable
            # generation boundary before handing it back to RQ.
            list_campaigns = getattr(store, "list_campaigns", None)
            if callable(list_campaigns):
                offset = 0
                while True:
                    page = list_campaigns(None, 200, offset)
                    if not page:
                        break
                    for campaign in page:
                        campaign_id = campaign.get("id") if isinstance(campaign, dict) else None
                        campaign = current_campaign(campaign_id, campaign) if isinstance(campaign_id, str) else None
                        if campaign is None or campaign.get("status") not in {"queued", "running"}:
                            continue
                        if campaign_id in accepted_campaigns:
                            continue
                        identity_generation = campaign.get("identity_generation")
                        if type(identity_generation) is not int or identity_generation < 0:
                            continue
                        if not needs_prepare(campaign):
                            continue
                        generation = store.pending_reconcile_prepare_generation(campaign_id)
                        if type(generation) is int and generation > 0:
                            enqueue_prepare(campaign_id, generation, campaign)
                            store.mark_reconcile_prepare_generation(campaign_id, generation)
                            continue
                        # An acknowledged generation can disappear when Redis is
                        # flushed.  Only then allocate g+1; if RQ still has the
                        # fixed gN job, leave it alone.  This remains prepare
                        # only and therefore cannot replay an approved submit.
                        current_generation = campaign.get("prepare_generation")
                        fetch = getattr(recovery_queue.queue, "fetch_job", None)
                        job = fetch(f"campaign-prepare-{campaign_id}-g{current_generation}") if callable(fetch) else None
                        job_status = None
                        get_status = getattr(job, "get_status", None)
                        if callable(get_status):
                            try:
                                job_status = get_status(refresh=True)
                            except Exception:
                                job_status = None
                        status_value = getattr(job_status, "value", job_status)
                        if isinstance(status_value, bytes):
                            status_value = status_value.decode("utf-8", "ignore")
                        status_name = (
                            str(status_value).rsplit(".", 1)[-1].casefold()
                            if status_value is not None
                            else ""
                        )
                        terminal = status_name in {
                            "finished", "failed", "stopped", "canceled", "cancelled"
                        }
                        if (
                            type(current_generation) is int and current_generation > 0
                            and callable(fetch) and (job is None or terminal)
                        ):
                            rebuilt_generation = store.next_prepare_generation(campaign_id)
                            current = current_campaign(campaign_id, campaign)
                            if current is None or type(current.get("identity_generation")) is not int:
                                continue
                            enqueue_prepare(campaign_id, rebuilt_generation, current)
                            store.mark_reconcile_prepare_generation(campaign_id, rebuilt_generation)
                    if len(page) < 200:
                        break
                    offset += len(page)
        worker_factory(queue_factory(connection), connection).work(with_scheduler=True)
    finally:
        stop.set()
        if "heartbeat_thread" in locals():
            heartbeat_thread.join(timeout=1)
        if "health_lease" in locals():
            try:
                health_lease.release()
            except RedisUnavailableError:
                pass
        close = getattr(store, "close", None)
        if callable(close):
            close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--project-fingerprint", required=True)
    parser.add_argument("--owner-nonce", required=True)
    args = parser.parse_args(argv)
    if args.command == "serve":
        current_fingerprint = project_fingerprint(PROJECT_ROOT)
        if args.project_fingerprint != current_fingerprint:
            raise RuntimeError("Comment Campaign worker project fingerprint mismatch")
        serve(
            expected_project_fingerprint=args.project_fingerprint,
            owner_nonce=args.owner_nonce,
        )


if __name__ == "__main__":
    main()
