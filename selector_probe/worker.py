"""Scheduled worker for manually managed selector validation."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import re
import signal
import threading
from typing import Callable
import uuid

from adspower import AdsPowerController
from gateway.settings_store import load_settings
from selector_probe.alerts import AlertService
from selector_probe.config import normalize_probe_config
from selector_probe.gates import StrategyGateService
from selector_probe.managed_runtime import ManagedElementRuntime, ManagedProbeRuntime
from selector_probe.probe import (
    LEASE_HEARTBEAT_SECONDS,
    LEASE_TTL_SECONDS,
    _LeaseHeartbeat,
    _sanitize_progress_event,
    run_managed_probe,
)
from selector_probe.registry import (
    RedisSelectorRegistry,
    reconcile_registry,
)
from selector_probe.redaction import DEFAULT_EVIDENCE_ROOT, delete_evidence_file
from selector_probe.scheduler import RedisLease, due_daily_slot
from selector_probe.store import SelectorProbeStore
from selector_probe.webhook import WebhookDispatcher


DEFAULT_INTERVAL_SECONDS = 30.0
STOP_CHECK_SECONDS = 0.5
INFRASTRUCTURE_RETRY_SECONDS = (900, 1800, 3600)
PROBE_STALE_SECONDS = 36 * 60 * 60
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
LOGGER = logging.getLogger("selector_probe.worker")
BUSINESS_STAGES = (
    "prepare_environment",
    "open_and_replay",
    "validate_elements",
    "protect_or_recover",
    "alert_and_cleanup",
)
REDIS_CONNECT_TIMEOUT_SECONDS = 3.0
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _redis_client(url: str, *, password: str = "") -> object:
    from redis import Redis

    kwargs = {
        "socket_connect_timeout": REDIS_CONNECT_TIMEOUT_SECONDS,
        "socket_timeout": REDIS_SOCKET_TIMEOUT_SECONDS,
    }
    if password:
        kwargs["password"] = password
    return Redis.from_url(url, **kwargs)


def _registry_factory(
    redis_client: object,
    *,
    environment: str,
    site: str,
    namespace: str = "selector_registry",
) -> RedisSelectorRegistry:
    return RedisSelectorRegistry(
        redis_client,
        environment=environment,
        site=site,
        namespace=namespace,
    )


def _reconcile_publication(store: object, registry: object) -> dict:
    if not callable(getattr(store, "claim_outbox_event", None)):
        return {"acknowledged": 0, "version": ""}
    return reconcile_registry(store, registry)


def _managed_runtime_factory(**kwargs) -> object:
    return ManagedProbeRuntime(**kwargs)


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", "")
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return "probe_unavailable"


def _log_failure(logger: object, error: BaseException) -> None:
    log_error = getattr(logger, "error", None)
    if callable(log_error):
        log_error(
            "selector_probe_tick_failed code=%s",
            _error_code(error),
        )


def _last_terminal_slot(store: object) -> datetime | None:
    method = getattr(store, "last_terminal_slot", None)
    if callable(method):
        return method()
    return store.last_completed_slot()


def _probe_run_state(
    store: object,
    slot: datetime,
) -> dict[str, object] | None:
    method = getattr(store, "probe_run_state", None)
    if not callable(method):
        return None
    value = method(slot.isoformat())
    return value if isinstance(value, dict) else None


def _retry_state(
    state: dict[str, object] | None,
) -> tuple[int, datetime | None]:
    if not state or state.get("status") not in {
        "infrastructure_unavailable",
        "probe_unavailable",
        "publication_failed",
    }:
        return 0, None
    details = state.get("details")
    if not isinstance(details, dict):
        return 0, None
    count = details.get("retry_count")
    retry_count = (
        count
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        else 0
    )
    raw_retry_at = details.get("retry_at")
    if not isinstance(raw_retry_at, str):
        return retry_count, None
    try:
        retry_at = datetime.fromisoformat(
            raw_retry_at.replace("Z", "+00:00")
        )
    except ValueError:
        return retry_count, None
    return retry_count, retry_at


def _retry_at(now: datetime, retry_count: int) -> datetime:
    from datetime import timedelta

    delay = INFRASTRUCTURE_RETRY_SECONDS[
        min(max(retry_count - 1, 0), len(INFRASTRUCTURE_RETRY_SECONDS) - 1)
    ]
    return now.astimezone(UTC) + timedelta(seconds=delay)


def _health_state(
    store: object,
    *,
    site: str,
    environment: str,
) -> dict[str, object]:
    method = getattr(store, "probe_health_state", None)
    if not callable(method):
        return {}
    value = method(site=site, environment=environment)
    return value if isinstance(value, dict) else {}


def _health_retry(
    health: dict[str, object],
) -> tuple[int, datetime | None]:
    count = health.get("retry_count")
    retry_count = (
        count
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        else 0
    )
    raw = health.get("next_retry_at")
    if not isinstance(raw, str) or not raw:
        return retry_count, None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return retry_count, None
    return retry_count, parsed


def _failure_started_at(
    health: dict[str, object],
    now: datetime,
) -> datetime:
    for field in ("last_validated_at", "failure_started_at"):
        raw = health.get(field)
        if isinstance(raw, str) and raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    return parsed.astimezone(UTC)
            except ValueError:
                pass
    return now.astimezone(UTC)


def _probe_policy(
    config: object,
    *,
    outcome: str,
    now: datetime,
) -> dict[str, object]:
    return {
        "site": config.site,
        "environment": config.environment,
        "outcome": outcome,
        "occurred_at": now.astimezone(UTC).isoformat(),
    }


def _capture_terminal_screenshot(
    runtime: object,
    *,
    failed_aliases: tuple[str, ...],
    evidence_root: Path,
    run_id: int,
) -> str:
    capture = getattr(runtime, "capture_failure_screenshot", None)
    if not callable(capture) or not failed_aliases:
        return ""
    target = evidence_root / f"selector-failure-{run_id}.jpg"
    if target.exists():
        try:
            if (
                target.is_file()
                and not target.is_symlink()
                and target.resolve(strict=True).parent
                == evidence_root.resolve(strict=True)
            ):
                return str(target)
        except (OSError, ValueError):
            return ""
    try:
        result = capture(
            failed_aliases=failed_aliases,
            target_path=target,
            evidence_root=evidence_root,
        )
    except Exception:
        LOGGER.warning("selector_probe_failure_screenshot_failed")
        return ""
    selected = Path(result) if isinstance(result, (str, Path)) else None
    if selected is None:
        return ""
    try:
        if selected.resolve(strict=True).parent != evidence_root.resolve(
            strict=True
        ):
            return ""
    except (OSError, ValueError):
        return ""
    return str(selected)


def _drain_probe_effects(
    *,
    store: object,
    gates: object,
    alerts: object,
    site: str,
    environment: str,
) -> list[dict[str, object]]:
    pending = getattr(store, "pending_probe_effects", None)
    apply = getattr(store, "apply_probe_effect", None)
    complete = getattr(store, "complete_probe_effect", None)
    if not all(callable(item) for item in (pending, apply, complete)):
        return []
    completed: list[dict[str, object]] = []
    for effect in pending(site=site, environment=environment):
        result = apply(
            effect["id"],
            site=site,
            environment=environment,
        )
        if not isinstance(result, dict):
            raise RuntimeError("probe effect result is invalid")
        screenshot_path = result.get("screenshot_path")
        alert_id = result.get("alert_id")
        if (
            isinstance(screenshot_path, str)
            and screenshot_path
            and isinstance(alert_id, int)
            and not isinstance(alert_id, bool)
        ):
            try:
                alerts.record_screenshot(
                    alert_id=alert_id,
                    path=screenshot_path,
                )
            except Exception:
                LOGGER.warning("selector_probe_alert_screenshot_link_failed")
                evidence_root = getattr(alerts, "evidence_root", None)
                if evidence_root is not None:
                    try:
                        delete_evidence_file(
                            evidence_root,
                            screenshot_path,
                        )
                    except Exception:
                        LOGGER.warning(
                            "selector_probe_orphan_screenshot_cleanup_failed"
                        )
        strategy_ids = result.get("strategy_ids", ())
        projector = getattr(gates, "project_strategy_ids", None)
        if callable(projector) and projector(strategy_ids) is not True:
            break
        if complete(
            effect["id"],
            site=site,
            environment=environment,
        ) is not True:
            raise RuntimeError("probe effect completion was fenced")
        completed.append(
            {
                "event_type": effect["event_type"],
                **result,
            }
        )
    return completed


def _reconcile_until(
    store: object,
    registry: object,
    reconcile_runner: Callable[[object, object], dict],
    version: str,
) -> bool:
    for _attempt in range(32):
        active = registry.get_active()
        if isinstance(active, dict) and active.get("version") == version:
            return True
        result = reconcile_runner(store, registry)
        if (
            isinstance(result, dict)
            and result.get("version") == version
        ):
            active = registry.get_active()
            return (
                isinstance(active, dict)
                and active.get("version") == version
            )
        if not isinstance(result, dict) or not any(
            result.get(key)
            for key in (
                "acknowledged",
                "repopulated",
                "retry_scheduled",
                "lease_lost",
                "conflict",
                "publication_failed",
            )
        ):
            return False
    return False


def _verified_recovery(
    *,
    result: dict[str, object],
    store: object,
    registry: object,
    reconcile_runner: Callable[[object, object], dict],
    heartbeat: object,
    config: object,
    run_id: int,
    attempt_token: str,
) -> tuple[dict[str, object], tuple[str, ...], str] | None:
    evidence = result.get("validation_evidence")
    if not isinstance(evidence, dict):
        return None
    active = registry.get_active()
    if not isinstance(active, dict):
        return None
    status = result.get("status")
    version = result.get("new_version")
    if status == "healthy":
        bundle_hash = active.get("bundle_hash")
        elements = active.get("elements")
        base_version = active.get("version")
        if (
            not isinstance(bundle_hash, str)
            or not isinstance(elements, dict)
            or not isinstance(base_version, str)
            or evidence.get("bundle_hash") != bundle_hash
        ):
            return None
        heartbeat.require_owned(renew=True)
        version = store.store_validated_version(
            bundle={
                "bundle_hash": bundle_hash,
                "elements": elements,
            },
            evidence=evidence,
            base_version_id=base_version,
            model_id="",
            prompt_version="",
            site=config.site,
            environment=config.environment,
            probe_run_id=run_id,
            attempt_token=attempt_token,
        )
        heartbeat.require_owned(renew=True)
        if not _reconcile_until(
            store,
            registry,
            reconcile_runner,
            version,
        ):
            return None
        result = {
            **result,
            "status": "published",
            "published": True,
            "new_version": version,
        }
        active = registry.get_active()
    elif status == "published":
        if not isinstance(version, str) or not version:
            return None
        if not _reconcile_until(
            store,
            registry,
            reconcile_runner,
            version,
        ):
            return None
        active = registry.get_active()
    else:
        return None
    if (
        not isinstance(active, dict)
        or active.get("version") != version
        or active.get("bundle_hash") != evidence.get("bundle_hash")
        or not isinstance(active.get("elements"), dict)
    ):
        return None
    stored = store.get_version(version)
    if (
        not isinstance(stored, dict)
        or stored.get("status") != "published"
        or stored.get("bundle_hash") != active.get("bundle_hash")
    ):
        return None
    return (
        result,
        tuple(sorted(active["elements"])),
        str(active["bundle_hash"]),
    )


def _record_healthy_evidence(
    store: object,
    *,
    run_id: int,
    attempt_token: str,
    result: dict,
) -> int:
    evidence = result.get("validation_evidence")
    if not isinstance(evidence, dict):
        return 0
    validations = evidence.get("validations")
    recorder = getattr(store, "record_validation", None)
    if not isinstance(validations, list) or not callable(recorder):
        return 0
    recorded = 0
    for validation in validations:
        if not isinstance(validation, dict):
            continue
        recorder(
            run_id=run_id,
            profile_mask=validation.get("profile_mask"),
            round_number=validation.get("round_number"),
            page_state="multi_state",
            result="passed",
            failure_code="",
            evidence={
                "bundle_hash": evidence.get("bundle_hash"),
                **validation,
            },
            attempt_token=attempt_token,
        )
        recorded += 1
    return recorded


def _run_maintenance(
    *,
    store: object,
    config: object,
    now: datetime,
    alert_service_factory: Callable[..., object],
    webhook_dispatcher_factory: Callable[..., object],
    evidence_root: Path,
) -> None:
    try:
        alerts = alert_service_factory(
            store,
            profile_ids=config.test_profile_ids,
            evidence_root=evidence_root,
        )
        alerts.cleanup_screenshots(now=now.isoformat(), retention_days=7)
    except Exception:
        LOGGER.warning("selector_probe_screenshot_cleanup_failed")
    webhook = config.webhook
    if webhook.enabled is not True:
        return
    try:
        dispatcher = webhook_dispatcher_factory(
            store=store,
            url=webhook.url,
            signing_secret=webhook.signing_secret,
            webhook_type=webhook.type,
        )
        dispatcher.deliver_due(now)
    except Exception:
        LOGGER.warning("selector_probe_webhook_delivery_failed")


def wake_element_request_worker(
    request_id: str,
    *,
    store_factory: Callable[[Path], object] = SelectorProbeStore,
    db_path: str | Path | None = None,
) -> dict[str, object]:
    """Reject removed element-request workflow explicitly."""

    del store_factory, db_path
    if (
        not isinstance(request_id, str)
        or not request_id
        or request_id != request_id.strip()
        or len(request_id) > 128
    ):
        raise ValueError("request_id is invalid")
    raise RuntimeError("element request workflow is removed")



def run_tick(
    *,
    settings_loader: Callable[[], dict] = load_settings,
    store_factory: Callable[[Path], object] = SelectorProbeStore,
    redis_factory: Callable[[str], object] = _redis_client,
    registry_factory: Callable[..., object] = _registry_factory,
    reconcile_runner: Callable[[object, object], dict] = (
        _reconcile_publication
    ),
    adspower_factory: Callable[..., object] = AdsPowerController,
    managed_runner: Callable[..., dict] = run_managed_probe,
    managed_runtime_factory: Callable[..., object] = (
        _managed_runtime_factory
    ),
    lease_factory: Callable[..., object] = RedisLease,
    gate_service_factory: Callable[..., object] = StrategyGateService,
    alert_service_factory: Callable[..., object] = AlertService,
    webhook_dispatcher_factory: Callable[..., object] = WebhookDispatcher,
    owner_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    db_path: str | Path | None = None,
    evidence_root: str | Path | None = None,
    redis_url: str | None = None,
    clock: object | None = None,
    stop_event: object | None = None,
    force: bool = False,
    management_request_id: str = "",
) -> dict:
    """Load current settings and run at most one due probe slot."""

    settings = settings_loader()
    if not isinstance(settings, dict):
        raise ValueError("settings loader must return a JSON object")
    config = normalize_probe_config(settings.get("selector_probe"))
    if not isinstance(force, bool):
        raise ValueError("force must be a boolean")
    adspower = settings.get("adspower", {})
    if not isinstance(adspower, dict):
        raise ValueError("adspower settings must be a JSON object")

    selected_db_path = Path(
        db_path
        or os.getenv("SELECTOR_PROBE_DB_PATH", "").strip()
        or PROJECT_ROOT / "data" / "selector-probe.db"
    )
    selected_db_path.parent.mkdir(parents=True, exist_ok=True)
    selected_evidence_root = Path(
        evidence_root
        or os.getenv("SELECTOR_PROBE_EVIDENCE_ROOT", "").strip()
        or DEFAULT_EVIDENCE_ROOT
    )
    selected_redis_url = (
        redis_url
        or str(
            (
                settings.get("selector_probe", {}).get("redis", {})
                if isinstance(settings.get("selector_probe"), dict)
                else {}
            ).get("url")
            or ""
        ).strip()
        or os.getenv("CELERY_BROKER_URL", "").strip()
        or "redis://127.0.0.1:6379/0"
    )
    redis_namespace = str(
        (
            settings.get("selector_probe", {}).get("redis", {})
            if isinstance(settings.get("selector_probe"), dict)
            else {}
        ).get("namespace")
        or "selector_registry"
    )
    if config.enabled is not True:
        selected_clock = clock or SystemClock()
        clock_now = getattr(selected_clock, "now", None)
        maintenance_now = (
            clock_now() if callable(clock_now) else SystemClock().now()
        )
        with store_factory(selected_db_path) as store:
            _run_maintenance(
                store=store,
                config=config,
                now=maintenance_now,
                alert_service_factory=alert_service_factory,
                webhook_dispatcher_factory=webhook_dispatcher_factory,
                evidence_root=selected_evidence_root,
            )
        return {
            "status": "disabled",
            "observe_only": config.observe_only,
        }
    redis_password = str(
        (
            settings.get("selector_probe", {}).get("redis", {})
            if isinstance(settings.get("selector_probe"), dict)
            else {}
        ).get("password")
        or ""
    )
    redis_client = (
        redis_factory(selected_redis_url, password=redis_password)
        if redis_factory is _redis_client
        else redis_factory(selected_redis_url)
    )
    try:
        with store_factory(selected_db_path) as store:
            selected_clock = clock or SystemClock()
            maintenance_clock = getattr(selected_clock, "now", None)
            maintenance_now = (
                maintenance_clock()
                if callable(maintenance_clock)
                else SystemClock().now()
            )
            if not isinstance(maintenance_now, datetime):
                raise ValueError("clock.now() must return a datetime")
            _run_maintenance(
                store=store,
                config=config,
                now=maintenance_now,
                alert_service_factory=alert_service_factory,
                webhook_dispatcher_factory=webhook_dispatcher_factory,
                evidence_root=selected_evidence_root,
            )
            attempt_token = owner_id_factory()
            lease = lease_factory(
                redis_client,
                (
                    f"{redis_namespace}:{config.environment}:"
                    f"{config.site}:lease"
                ),
                attempt_token,
                ttl_seconds=LEASE_TTL_SECONDS,
                heartbeat_seconds=LEASE_HEARTBEAT_SECONDS,
            )
            if lease.acquire() is not True:
                return {
                    "status": "lease_busy",
                    "observe_only": config.observe_only,
                }
            heartbeat = _LeaseHeartbeat(lease)
            heartbeat.start()
            run_id: int | None = None
            stage_map: dict[
                tuple[str, str, object], dict[str, object]
            ] = {}
            diagnostic_map: dict[
                tuple[str, str, object], dict[str, object]
            ] = {}

            def record_progress(event: Mapping[str, object]) -> None:
                sanitized = _sanitize_progress_event(event)
                key = (
                    str(sanitized["name"]),
                    str(sanitized["profile_mask"]),
                    sanitized.get("round"),
                )
                target = (
                    stage_map
                    if sanitized["name"] in BUSINESS_STAGES
                    else diagnostic_map
                )
                target[key] = sanitized
                while len(target) > 30:
                    target.pop(next(iter(target)))
                updater = getattr(store, "update_run_progress", None)
                if callable(updater) and run_id is not None:
                    try:
                        updater(
                            run_id,
                            attempt_token=attempt_token,
                            stages=list(stage_map.values()),
                        )
                    except Exception:
                        pass

            try:
                registry_kwargs = {
                    "environment": config.environment,
                    "site": config.site,
                }
                if registry_factory is _registry_factory:
                    registry_kwargs["namespace"] = redis_namespace
                registry = registry_factory(
                    redis_client, **registry_kwargs
                )
                gates = gate_service_factory(
                    store,
                    redis_client=redis_client,
                    environment=config.environment,
                    site=config.site,
                )
                alerts = alert_service_factory(
                    store,
                    profile_ids=config.test_profile_ids,
                    evidence_root=selected_evidence_root,
                )
                _drain_probe_effects(
                    store=store,
                    gates=gates,
                    alerts=alerts,
                    site=config.site,
                    environment=config.environment,
                )
                if not config.observe_only:
                    reconcile_runner(store, registry)
                now = selected_clock.now()
                if not isinstance(now, datetime):
                    raise ValueError("clock.now() must return a datetime")
                slot = (
                    now
                    if force
                    else due_daily_slot(
                        now,
                        _last_terminal_slot(store),
                        config.timezone,
                        config.daily_time,
                    )
                )
                if slot is None and not force:
                    return {
                        "status": "not_due",
                        "observe_only": config.observe_only,
                    }
                health = _health_state(
                    store,
                    site=config.site,
                    environment=config.environment,
                )
                retry_count, retry_at = _health_retry(health)
                if slot is not None:
                    if not health:
                        retry_count, retry_at = _retry_state(
                            _probe_run_state(store, slot)
                        )
                    if (
                        not force
                        and retry_at is not None
                        and now.astimezone(UTC) < retry_at.astimezone(UTC)
                    ):
                        return {
                            "status": "retry_wait",
                            "observe_only": config.observe_only,
                            "retry_at": retry_at.isoformat(),
                        }
                try:
                    active = registry.get_active()
                except Exception:
                    active = None
                active_version = (
                    active.get("version", "")
                    if isinstance(active, dict)
                    else ""
                )
                scheduled_for = slot or now
                run_id = store.start_run(
                    scheduled_for=scheduled_for.isoformat(),
                    active_version_before=active_version,
                    attempt_token=attempt_token,
                    management_request_id=management_request_id,
                    trigger="manual" if management_request_id else "scheduled",
                )
                for business_stage in BUSINESS_STAGES:
                    record_progress(
                        {
                            "name": business_stage,
                            "status": (
                                "running"
                                if business_stage == "prepare_environment"
                                else "queued"
                            ),
                            "attempt_count": 1,
                        }
                    )
                candidate_runtime = ManagedElementRuntime(store)
                try:
                    candidate = candidate_runtime.load_candidate()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result = {
                        "status": "infrastructure_unavailable",
                        "failure_code": "candidate_unavailable",
                        "published": False,
                        "new_version": None,
                        "proposed_pause_aliases": [],
                    }
                else:
                    raw_elements = candidate.get("elements")
                    if not raw_elements:
                        result = managed_runner(
                            candidate_runtime,
                            publish=not config.observe_only,
                            candidate=candidate,
                        )
                    else:
                        result = None
                adspower_client = adspower_factory(
                    base_url=adspower.get("base_url"),
                    api_key=adspower.get("api_key"),
                    timeout=5.0,
                    max_retries=1,
                ) if result is None else None
                if result is None:
                    runtime_kwargs = {
                        "config": config,
                        "settings": settings,
                        "store": store,
                        "registry": registry,
                        "adspower_client": adspower_client,
                        "stop_event": stop_event,
                        "lease_guard": heartbeat.require_owned,
                        "probe_run_id": run_id,
                        "attempt_token": attempt_token,
                        "progress_sink": record_progress,
                        "reconciler": reconcile_runner,
                        "page_ready_timeout_seconds": (
                            config.page_timeout_seconds
                        ),
                    }
                    runtime = managed_runtime_factory(**runtime_kwargs)
                    if isinstance(raw_elements, Mapping) and raw_elements:
                        with runtime as opened_runtime:
                            result = managed_runner(
                                opened_runtime,
                                publish=not config.observe_only,
                                candidate=candidate,
                            )
                            if not isinstance(result, dict):
                                raise RuntimeError(
                                    "managed probe result must be an object"
                                )
                            if result.get("status") == "selector_validation_failed":
                                failed_aliases = tuple(
                                    sorted(
                                        {
                                            alias
                                            for alias in result.get(
                                                "proposed_pause_aliases",
                                                (),
                                            )
                                            if isinstance(alias, str) and alias
                                        }
                                    )
                                )
                                screenshot_path = _capture_terminal_screenshot(
                                    opened_runtime,
                                    failed_aliases=failed_aliases,
                                    evidence_root=selected_evidence_root,
                                    run_id=run_id,
                                )
                                if screenshot_path:
                                    result["screenshot_path"] = screenshot_path
                if not isinstance(result, dict):
                    raise RuntimeError("managed probe result must be an object")
                if (
                    result.get("status") == "published"
                    and isinstance(result.get("new_version"), str)
                    and result["new_version"]
                ):
                    stored_version = store.get_version(
                        result["new_version"]
                    )
                    if (
                        isinstance(stored_version, Mapping)
                        and isinstance(
                            stored_version.get("evidence"), Mapping
                        )
                    ):
                        result["validation_evidence"] = dict(
                            stored_version["evidence"]
                        )
                result_status = str(
                    result.get("status") or "probe_unavailable"
                )
                if result_status in {"healthy", "published"}:
                    for business_stage in BUSINESS_STAGES:
                        record_progress(
                            {
                                "name": business_stage,
                                "status": "passed",
                                "attempt_count": 1,
                            }
                        )
                elif result_status == "selector_validation_failed":
                    for business_stage, business_status in (
                        ("prepare_environment", "passed"),
                        ("open_and_replay", "passed"),
                        ("validate_elements", "failed"),
                        ("protect_or_recover", "passed"),
                        ("alert_and_cleanup", "passed"),
                    ):
                        record_progress(
                            {
                                "name": business_stage,
                                "status": business_status,
                                "attempt_count": result.get(
                                    "attempt_count", 1
                                ),
                                "failure_code": (
                                    str(result.get("failure_code") or "")
                                    if business_status == "failed"
                                    else ""
                                ),
                            }
                        )
                elif result_status == "publication_failed":
                    for business_stage, business_status in (
                        ("prepare_environment", "passed"),
                        ("open_and_replay", "passed"),
                        ("validate_elements", "passed"),
                        ("protect_or_recover", "failed"),
                        ("alert_and_cleanup", "passed"),
                    ):
                        record_progress(
                            {
                                "name": business_stage,
                                "status": business_status,
                                "attempt_count": 1,
                                "failure_code": (
                                    str(result.get("failure_code") or "")
                                    if business_status == "failed"
                                    else ""
                                ),
                            }
                        )
                elif result_status == "awaiting_element_selection":
                    for business_stage in BUSINESS_STAGES:
                        record_progress(
                            {
                                "name": business_stage,
                                "status": "skipped",
                                "attempt_count": 1,
                            }
                        )
                heartbeat.require_owned(renew=True)
                status = result_status
                recovery: tuple[
                    dict[str, object],
                    tuple[str, ...],
                    str,
                ] | None = None
                recovery_pending = getattr(store, "recovery_pending", None)
                if (
                    not config.observe_only
                    and status in {"healthy", "published"}
                    and callable(recovery_pending)
                    and recovery_pending(
                        site=config.site,
                        environment=config.environment,
                    )
                ):
                    recovery = _verified_recovery(
                        result=result,
                        store=store,
                        registry=registry,
                        reconcile_runner=reconcile_runner,
                        heartbeat=heartbeat,
                        config=config,
                        run_id=run_id,
                        attempt_token=attempt_token,
                    )
                    if recovery is None:
                        result = {
                            **result,
                            "status": "publication_failed",
                            "published": False,
                            "failure_code": "recovery_verification_failed",
                        }
                    else:
                        result = recovery[0]
                    status = str(result["status"])
                validation_records = _record_healthy_evidence(
                    store,
                    run_id=run_id,
                    attempt_token=attempt_token,
                    result=result,
                )
                infrastructure = status not in {
                    "healthy",
                    "published",
                    "selector_validation_failed",
                    "awaiting_element_selection",
                }
                next_retry_count = retry_count + 1 if infrastructure else 0
                policy_outcome = (
                    "selector_failure"
                    if status == "selector_validation_failed"
                    else (
                        "validated"
                        if status in {"healthy", "published"}
                        else (
                            "infrastructure"
                            if status != "awaiting_element_selection"
                            else None
                        )
                    )
                )
                failed_aliases = tuple(
                    sorted(
                        {
                            alias
                            for alias in result.get(
                                "proposed_pause_aliases",
                                (),
                            )
                            if isinstance(alias, str) and alias
                        }
                    )
                )
                effect: dict[str, object] | None = None
                if (
                    not config.observe_only
                    and status == "selector_validation_failed"
                    and failed_aliases
                ):
                    effect = {
                        "key": (
                            f"probe-run:{run_id}:attempt:{attempt_token}:"
                            "selector-failure"
                        ),
                        "type": "selector_failure",
                        "payload": {
                            "site": config.site,
                            "environment": config.environment,
                            "aliases": list(failed_aliases),
                            "active_version": active_version,
                            "failure_code": str(
                                result.get("failure_code")
                                or "selector_validation_failed"
                            ),
                            "screenshot_path": str(
                                result.get("screenshot_path") or ""
                            ),
                            "occurred_at": now.astimezone(UTC).isoformat(),
                        },
                    }
                elif infrastructure and not config.observe_only:
                    failure_started = _failure_started_at(health, now)
                    elapsed = (
                        now.astimezone(UTC) - failure_started
                    ).total_seconds()
                    event_type = (
                        "probe_stale"
                        if elapsed >= PROBE_STALE_SECONDS
                        else "probe_unavailable"
                    )
                    effect = {
                        "key": (
                            f"probe-run:{run_id}:attempt:{attempt_token}:"
                            f"{event_type.replace('_', '-')}"
                        ),
                        "type": event_type,
                        "payload": {
                            "site": config.site,
                            "environment": config.environment,
                            "active_version": active_version,
                            "failure_started_at": failure_started.isoformat(),
                            "failure_code": str(
                                result.get("failure_code")
                                or "probe_unavailable"
                            ),
                            "occurred_at": now.astimezone(UTC).isoformat(),
                        },
                    }
                elif recovery is not None:
                    _recovered_result, covered_aliases, bundle_hash = recovery
                    effect = {
                        "key": (
                            f"probe-run:{run_id}:attempt:{attempt_token}:"
                            "recovery"
                        ),
                        "type": "recovery",
                        "payload": {
                            "site": config.site,
                            "environment": config.environment,
                            "selector_version_id": str(
                                result.get("new_version") or ""
                            ),
                            "bundle_hash": bundle_hash,
                            "covered_aliases": list(covered_aliases),
                            "occurred_at": now.astimezone(UTC).isoformat(),
                        },
                    }
                store.finish_run(
                    run_id,
                    status=(
                        "completed"
                        if status in {"healthy", "published"}
                        else status
                    ),
                    details={
                        "observe_only": config.observe_only,
                        "status": status,
                        "published": result.get("published") is True,
                        "failure_code": result.get("failure_code", ""),
                        "validation_records": validation_records,
                        "stages": list(stage_map.values()),
                        "diagnostics": list(diagnostic_map.values()),
                        **(
                            {
                                "retry_count": next_retry_count,
                                "retry_at": _retry_at(
                                    now,
                                    next_retry_count,
                                ).isoformat(),
                            }
                            if infrastructure
                            else {}
                        ),
                    },
                    published_version_after=(
                        result.get("new_version")
                        if isinstance(result.get("new_version"), str)
                        else ""
                    ),
                    failed_aliases=result.get(
                        "proposed_pause_aliases",
                        (),
                    ),
                    attempt_token=attempt_token,
                    policy=(
                        _probe_policy(
                            config,
                            outcome=policy_outcome,
                            now=now,
                        )
                        if policy_outcome is not None
                        else None
                    ),
                    effect=effect,
                )
                completed_effects = _drain_probe_effects(
                    store=store,
                    gates=gates,
                    alerts=alerts,
                    site=config.site,
                    environment=config.environment,
                )
                paused = sorted(
                    {
                        strategy_id
                        for item in completed_effects
                        if item.get("event_type") in {
                            "selector_failure",
                            "probe_stale",
                        }
                        for strategy_id in item.get("strategy_ids", ())
                        if isinstance(strategy_id, str)
                    }
                )
                if paused:
                    result["paused_strategies"] = paused
                result["observe_only"] = config.observe_only
                return result
            except BaseException as error:
                if run_id is not None:
                    prepare_stage = stage_map.get(
                        ("prepare_environment", "", None), {}
                    )
                    if prepare_stage.get("status") != "passed":
                        record_progress(
                            {
                                "name": "prepare_environment",
                                "status": "failed",
                                "attempt_count": 1,
                                "failure_code": _error_code(error),
                            }
                        )
                    next_retry_count = retry_count + 1
                    failure_now = selected_clock.now()
                    failure_started = _failure_started_at(health, failure_now)
                    elapsed = (
                        failure_now.astimezone(UTC) - failure_started
                    ).total_seconds()
                    event_type = (
                        "probe_stale"
                        if elapsed >= PROBE_STALE_SECONDS
                        else "probe_unavailable"
                    )
                    effect = None
                    if not config.observe_only:
                        effect = {
                            "key": (
                                f"probe-run:{run_id}:attempt:{attempt_token}:"
                                f"{event_type.replace('_', '-')}"
                            ),
                            "type": event_type,
                            "payload": {
                                "site": config.site,
                                "environment": config.environment,
                                "active_version": active_version,
                                "failure_started_at": (
                                    failure_started.isoformat()
                                ),
                                "failure_code": _error_code(error),
                                "occurred_at": failure_now.astimezone(
                                    UTC
                                ).isoformat(),
                            },
                        }
                    store.finish_run(
                        run_id,
                        status="probe_unavailable",
                        details={
                            "observe_only": config.observe_only,
                            "failure_code": _error_code(error),
                            "retry_count": next_retry_count,
                            "retry_at": _retry_at(
                                selected_clock.now(),
                                next_retry_count,
                            ).isoformat(),
                            "stages": list(stage_map.values()),
                            "diagnostics": list(
                                diagnostic_map.values()
                            ),
                        },
                        attempt_token=attempt_token,
                        policy=_probe_policy(
                            config,
                            outcome="infrastructure",
                            now=failure_now,
                        ),
                        effect=effect,
                    )
                    _drain_probe_effects(
                        store=store,
                        gates=gates,
                        alerts=alerts,
                        site=config.site,
                        environment=config.environment,
                    )
                raise
            finally:
                heartbeat.stop()
                try:
                    lease.release()
                except Exception:
                    pass
    finally:
        close = getattr(redis_client, "close", None)
        if callable(close):
            close()


def _stopped(stop_event: object, stop_file: Path | None) -> bool:
    is_set = getattr(stop_event, "is_set", None)
    return (
        (callable(is_set) and is_set())
        or (stop_file is not None and stop_file.exists())
    )


def serve(
    *,
    settings_loader: Callable[[], dict] = load_settings,
    tick_runner: Callable[..., dict] = run_tick,
    stop_event: object | None = None,
    stop_file: str | Path | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    check_seconds: float = STOP_CHECK_SECONDS,
    logger: object = LOGGER,
) -> int:
    """Run probe ticks until a signal event or stop file requests shutdown."""

    if interval_seconds <= 0 or check_seconds <= 0:
        raise ValueError("worker intervals must be positive")
    event = stop_event or threading.Event()
    selected_stop_file = Path(stop_file) if stop_file else None
    stop_file_watcher_done = threading.Event()
    stop_file_watcher: threading.Thread | None = None
    if (
        selected_stop_file is not None
        and not _stopped(event, selected_stop_file)
    ):
        def watch_stop_file() -> None:
            while not stop_file_watcher_done.wait(check_seconds):
                if selected_stop_file.exists():
                    set_event = getattr(event, "set", None)
                    if callable(set_event):
                        set_event()
                    return

        stop_file_watcher = threading.Thread(
            target=watch_stop_file,
            name="selector-probe-stop-file-watcher",
            daemon=True,
        )
        stop_file_watcher.start()
    try:
        while not _stopped(event, selected_stop_file):
            try:
                tick_runner(
                    settings_loader=settings_loader,
                    stop_event=event,
                )
            except asyncio.CancelledError:
                if _stopped(event, selected_stop_file):
                    break
                raise
            except Exception as error:
                _log_failure(logger, error)
            if _stopped(event, selected_stop_file):
                break
            remaining = interval_seconds
            while remaining > 0 and not _stopped(
                event,
                selected_stop_file,
            ):
                duration = min(check_seconds, remaining)
                wait = getattr(event, "wait", None)
                if callable(wait) and wait(duration):
                    break
                remaining -= duration
    finally:
        stop_file_watcher_done.set()
        if stop_file_watcher is not None:
            stop_file_watcher.join(1.0)
    return 0


def _serve_cli() -> int:
    stop_event = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        selected_signal = getattr(signal, signal_name, None)
        if selected_signal is not None:
            signal.signal(selected_signal, request_stop)

    stop_file_value = os.getenv("SELECTOR_PROBE_STOP_FILE", "").strip()
    return serve(
        stop_event=stop_event,
        stop_file=stop_file_value or None,
    )


def main(
    argv: list[str] | None = None,
    *,
    tick_runner: Callable[[], dict] | None = None,
    serve_runner: Callable[[], int] | None = None,
    logger: object = LOGGER,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m selector_probe.worker")
    parser.add_argument("command", choices=("serve", "tick"))
    args = parser.parse_args(argv)

    if args.command == "serve":
        return (serve_runner or _serve_cli)()
    try:
        (tick_runner or run_tick)()
    except Exception as error:
        _log_failure(logger, error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_tick", "serve"]
