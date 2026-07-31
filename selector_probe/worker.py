"""Standalone observe-only selector probe worker."""

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
from browser_element_schema import normalize_element_definitions
from gateway.settings_store import load_settings
from selector_probe.alerts import AlertService
from selector_probe.config import normalize_probe_config
from selector_probe.contracts import default_tiktok_contracts
from selector_probe.gates import StrategyGateService
from selector_probe.healing_runtime import HealingRuntime
from selector_probe.probe import (
    LEASE_HEARTBEAT_SECONDS,
    LEASE_TTL_SECONDS,
    _LeaseHeartbeat,
    run_element_probe,
    run_healing_probe,
    run_observe_probe,
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
ELEMENT_REQUEST_TERMINAL_FAILURES = frozenset(
    {
        "draft_not_published",
        "observe_only_validation_disabled",
        "probe_disabled",
        "selector_validation_failed",
    }
)
ELEMENT_REQUEST_CLAIM_RENEW_SECONDS = 120
REDIS_CONNECT_TIMEOUT_SECONDS = 3.0
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0
ELEMENT_REQUEST_RECONCILE_IO_BUDGET_SECONDS = (
    REDIS_CONNECT_TIMEOUT_SECONDS + (4 * REDIS_SOCKET_TIMEOUT_SECONDS)
)
if (
    ELEMENT_REQUEST_RECONCILE_IO_BUDGET_SECONDS
    >= ELEMENT_REQUEST_CLAIM_RENEW_SECONDS
):
    raise RuntimeError("element request reconcile timeout exceeds claim window")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ElementRequestClaimLost(RuntimeError):
    code = "claim_lost"

    def __init__(self) -> None:
        super().__init__(self.code)


class ElementRequestRolloutDisabled(RuntimeError):
    code = "rollout_disabled"

    def __init__(self) -> None:
        super().__init__(self.code)


class ElementRequestProbeDisabled(RuntimeError):
    code = "probe_disabled"

    def __init__(self) -> None:
        super().__init__(self.code)


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


def _healing_runtime_factory(**kwargs) -> object:
    return HealingRuntime(**kwargs)


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
            prompt_version="selector-recovery-v1",
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


def consume_element_requests(
    *,
    store_factory: Callable[[Path], object] = SelectorProbeStore,
    executor: Callable[[Mapping[str, object]], object] | None = None,
    db_path: str | Path | None = None,
    owner_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    clock: object | None = None,
    max_requests: int = 10,
    claim_lease_seconds: int = 120,
    heartbeat_seconds: float | None = None,
) -> dict[str, int]:
    if (
        isinstance(max_requests, bool)
        or not isinstance(max_requests, int)
        or max_requests < 1
        or max_requests > 100
    ):
        raise ValueError("max_requests is invalid")
    if (
        isinstance(claim_lease_seconds, bool)
        or not isinstance(claim_lease_seconds, int)
        or claim_lease_seconds < 1
        or claim_lease_seconds > 3600
    ):
        raise ValueError("claim_lease_seconds is invalid")
    heartbeat_interval = heartbeat_seconds or min(
        30.0,
        claim_lease_seconds / 3,
    )
    if (
        isinstance(heartbeat_interval, bool)
        or not isinstance(heartbeat_interval, (int, float))
        or heartbeat_interval <= 0
        or heartbeat_interval >= claim_lease_seconds
    ):
        raise ValueError("heartbeat_seconds is invalid")
    selected_db_path = Path(
        db_path
        or os.getenv("SELECTOR_PROBE_DB_PATH", "").strip()
        or PROJECT_ROOT / "data" / "selector-probe.db"
    )
    selected_db_path.parent.mkdir(parents=True, exist_ok=True)
    selected_clock = clock or SystemClock()
    selected_executor = executor or run_element_request_runtime
    counts = {"claimed": 0, "completed": 0, "failed": 0, "retried": 0}
    with store_factory(selected_db_path) as store:
        claim = getattr(store, "claim_element_request", None)
        if not callable(claim):
            return counts
        for _item in range(max_requests):
            now = selected_clock.now()
            request = claim(
                claim_token=owner_id_factory(),
                now=now,
                lease_seconds=claim_lease_seconds,
            )
            if request is None:
                break
            counts["claimed"] += 1
            request_id = str(request["request_id"])
            claim_token = str(request["claim_token"])
            claim_generation = int(request["claim_generation"])
            if not store.element_request_claim_is_current(
                request_id,
                claim_token,
                claim_generation,
            ):
                outcome = store.fail_element_request(
                    request_id,
                    claim_token,
                    claim_generation,
                    error_code="stale_revision",
                    retryable=False,
                    now=selected_clock.now(),
                )
                if outcome is not None:
                    counts["failed"] += 1
                continue
            heartbeat_stop = threading.Event()
            heartbeat_lost = threading.Event()

            def renew_claim() -> None:
                while not heartbeat_stop.wait(heartbeat_interval):
                    try:
                        with store_factory(selected_db_path) as renew_store:
                            renewed = renew_store.renew_element_request_claim(
                                request_id,
                                claim_token,
                                claim_generation,
                                lease_seconds=claim_lease_seconds,
                            )
                    except BaseException:
                        renewed = False
                    if not renewed:
                        heartbeat_lost.set()
                        return

            heartbeat = threading.Thread(
                target=renew_claim,
                name=f"selector-element-lease-{request_id[:12]}",
                daemon=True,
            )
            heartbeat.start()
            try:
                try:
                    result = selected_executor(request)
                    if not isinstance(result, Mapping):
                        raise RuntimeError("element_request_result_invalid")
                    status = str(result.get("status") or "")
                    request_succeeded = (
                        request["request_type"] == "probe"
                        and status == "probe_completed"
                    ) or (
                        request["request_type"] == "validate"
                        and status == "published"
                        and result.get("published") is True
                        and result.get("reconciled") is True
                        and isinstance(result.get("new_version"), str)
                        and bool(result["new_version"])
                    )
                    if request_succeeded:
                        completed = (
                            not heartbeat_lost.is_set()
                            and store.complete_element_request(
                                request_id,
                                claim_token,
                                claim_generation,
                                result=dict(result),
                                now=selected_clock.now(),
                            )
                        )
                        if completed:
                            counts["completed"] += 1
                        elif not heartbeat_lost.is_set():
                            terminal = store.get_element_request(request_id)
                            if (
                                isinstance(terminal, Mapping)
                                and terminal.get("status") == "completed"
                            ):
                                counts["completed"] += 1
                            elif (
                                isinstance(terminal, Mapping)
                                and terminal.get("status") == "failed"
                            ):
                                counts["failed"] += 1
                        continue
                    error_code = str(
                        result.get("failure_code")
                        or (
                            "draft_not_published"
                            if request["request_type"] == "validate"
                            and status in {"completed", "healthy", "passed"}
                            else status
                        )
                        or "probe_unavailable"
                    )
                    retryable = (
                        error_code not in ELEMENT_REQUEST_TERMINAL_FAILURES
                    )
                except BaseException as error:
                    error_code = _error_code(error)
                    retryable = True
            finally:
                heartbeat_stop.set()
                heartbeat.join(min(heartbeat_interval + 0.5, 2.0))
            if heartbeat_lost.is_set():
                continue
            outcome = store.fail_element_request(
                request_id,
                claim_token,
                claim_generation,
                error_code=error_code,
                retryable=retryable,
                now=selected_clock.now(),
            )
            if outcome is not None:
                counts[
                    "failed" if outcome["terminal"] else "retried"
                ] += 1
    return counts


def run_element_request_runtime(
    request: Mapping[str, object],
    *,
    settings_loader: Callable[[], dict] = load_settings,
    store_factory: Callable[[Path], object] = SelectorProbeStore,
    redis_factory: Callable[[str], object] = _redis_client,
    registry_factory: Callable[..., object] = _registry_factory,
    adspower_factory: Callable[..., object] = AdsPowerController,
    healing_runtime_factory: Callable[..., object] = _healing_runtime_factory,
    lease_factory: Callable[..., object] = RedisLease,
    db_path: str | Path | None = None,
    redis_url: str | None = None,
) -> dict[str, object]:
    if not isinstance(request, Mapping):
        raise ValueError("element request must be an object")
    request_type = request.get("request_type")
    element_id = request.get("element_id")
    contract = request.get("contract")
    request_id = request.get("request_id")
    claim_token = request.get("claim_token")
    claim_generation = request.get("claim_generation")
    if (
        request_type not in {"probe", "validate"}
        or not isinstance(element_id, str)
        or not element_id
        or not isinstance(contract, Mapping)
        or not contract
        or not isinstance(request_id, str)
        or not request_id
        or not isinstance(claim_token, str)
        or not claim_token
        or isinstance(claim_generation, bool)
        or not isinstance(claim_generation, int)
        or claim_generation < 1
    ):
        raise ValueError("element request is invalid")
    settings = settings_loader()
    if not isinstance(settings, dict):
        raise ValueError("settings loader must return a JSON object")
    config = normalize_probe_config(settings.get("selector_probe"))
    if config.enabled is not True:
        return {
            "status": "probe_disabled",
            "failure_code": "probe_disabled",
        }
    if request_type == "validate" and config.observe_only:
        return {
            "status": "observe_only_validation_disabled",
            "failure_code": "observe_only_validation_disabled",
        }
    browser = settings.get("browser", {})
    elements = normalize_element_definitions(
        browser.get("action_elements", {})
        if isinstance(browser, dict)
        else {}
    )
    selected_db_path = Path(
        db_path
        or os.getenv("SELECTOR_PROBE_DB_PATH", "").strip()
        or PROJECT_ROOT / "data" / "selector-probe.db"
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
    adspower = settings.get("adspower", {})
    if not isinstance(adspower, dict):
        raise ValueError("adspower settings must be a JSON object")
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
            registry_kwargs = {
                "environment": config.environment,
                "site": config.site,
            }
            if registry_factory is _registry_factory:
                registry_kwargs["namespace"] = redis_namespace
            registry = registry_factory(redis_client, **registry_kwargs)
            owner = uuid.uuid4().hex
            lease = lease_factory(
                redis_client,
                (
                    f"{redis_namespace}:{config.environment}:"
                    f"{config.site}:lease"
                ),
                owner,
                ttl_seconds=LEASE_TTL_SECONDS,
                heartbeat_seconds=LEASE_HEARTBEAT_SECONDS,
            )
            if lease.acquire() is not True:
                return {
                    "status": "lease_busy",
                    "failure_code": "lease_busy",
                }
            heartbeat = _LeaseHeartbeat(lease)
            heartbeat.start()
            try:
                claim_guard = getattr(
                    store,
                    "guard_element_request_claim",
                    None,
                )
                if not callable(claim_guard):
                    raise RuntimeError(
                        "element request claim guard is unavailable"
                    )

                def critical_guard(*, renew: bool = False) -> None:
                    if renew and request_type == "validate":
                        fresh_settings = settings_loader()
                        if not isinstance(fresh_settings, dict):
                            raise ValueError(
                                "settings loader must return a JSON object"
                            )
                        fresh_config = normalize_probe_config(
                            fresh_settings.get("selector_probe")
                        )
                        if fresh_config.enabled is not True:
                            raise ElementRequestProbeDisabled()
                        if fresh_config.observe_only:
                            raise ElementRequestRolloutDisabled()
                    guarded = claim_guard(
                        request_id,
                        claim_token,
                        claim_generation,
                        renew=renew,
                        lease_seconds=ELEMENT_REQUEST_CLAIM_RENEW_SECONDS,
                    )
                    if guarded is not True:
                        raise ElementRequestClaimLost()
                    heartbeat.require_owned(renew=renew)

                critical_guard(renew=True)
                runtime = healing_runtime_factory(
                    config=config,
                    settings=settings,
                    store=store,
                    registry=registry,
                    adspower_client=adspower_factory(
                        base_url=adspower.get("base_url"),
                        api_key=adspower.get("api_key"),
                        timeout=5.0,
                        max_retries=1,
                    ),
                    elements=elements,
                    stop_event=None,
                    lease_guard=critical_guard,
                    element_request_id=(
                        request_id if request_type == "validate" else ""
                    ),
                    element_request_claim_token=(
                        claim_token if request_type == "validate" else ""
                    ),
                    element_request_generation=(
                        claim_generation if request_type == "validate" else 0
                    ),
                    contracts_override={element_id: dict(contract)},
                )
                with runtime as opened_runtime:
                    result = (
                        run_element_probe(opened_runtime)
                        if request_type == "probe"
                        else run_healing_probe(
                            opened_runtime,
                            force_requested_candidate=True,
                            initial_failed_aliases=(element_id,),
                        )
                    )
                if not isinstance(result, dict):
                    raise RuntimeError("element request result is invalid")
                if (
                    request_type == "validate"
                    and result.get("status") == "published"
                    and isinstance(result.get("new_version"), str)
                ):
                    if not store.element_request_publication_is_complete(
                        request_id,
                        claim_generation,
                        result["new_version"],
                    ):
                        raise ElementRequestClaimLost()
                else:
                    critical_guard(renew=True)
                return result
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


def wake_element_request_worker(
    request_id: str,
    *,
    store_factory: Callable[[Path], object] = SelectorProbeStore,
    db_path: str | Path | None = None,
) -> dict[str, object]:
    if (
        not isinstance(request_id, str)
        or not request_id
        or request_id != request_id.strip()
        or len(request_id) > 128
    ):
        raise ValueError("request_id is invalid")

    def run() -> None:
        try:
            consume_element_requests(
                store_factory=store_factory,
                executor=lambda request: run_element_request_runtime(
                    request,
                    store_factory=store_factory,
                    db_path=db_path,
                ),
                db_path=db_path,
                max_requests=10,
            )
        except BaseException:
            LOGGER.warning("element_request_wakeup_failed")

    thread = threading.Thread(
        target=run,
        name=f"selector-element-request-{request_id[:12]}",
        daemon=True,
    )
    thread.start()
    return {"status": "woken", "request_id": request_id}


def _settle_disabled_element_publications(
    *,
    store_factory: Callable[[Path], object],
    db_path: Path,
    redis_factory: Callable[[str], object],
    registry_factory: Callable[..., object],
    redis_url: str,
    environment: str,
    site: str,
    error_code: str,
    now: datetime,
) -> dict[str, int]:
    with store_factory(db_path) as store:
        abort = getattr(
            store,
            "abort_disabled_element_publications",
            None,
        )
        if not callable(abort):
            return {"aborted": 0, "inflight": 0, "indeterminate": 0}
        outcome = abort(error_code=error_code, now=now)
    if not outcome["indeterminate"]:
        return outcome
    redis_client = redis_factory(redis_url)
    try:
        registry = registry_factory(
            redis_client,
            environment=environment,
            site=site,
        )
        try:
            active = registry.get_active()
        except Exception:
            return {
                **outcome,
                "unresolved": outcome["indeterminate"],
            }
        active_version = ""
        active_bundle_hash = ""
        if isinstance(active, Mapping):
            version = active.get("version")
            bundle_hash = active.get("bundle_hash")
            if isinstance(version, str):
                active_version = version
            if isinstance(bundle_hash, str):
                active_bundle_hash = bundle_hash
        with store_factory(db_path) as store:
            resolve = getattr(
                store,
                "resolve_indeterminate_element_publications",
                None,
            )
            if not callable(resolve):
                return {
                    **outcome,
                    "unresolved": outcome["indeterminate"],
                }
            resolution = resolve(
                active_version=active_version,
                active_bundle_hash=active_bundle_hash,
                error_code=error_code,
                now=now,
            )
        return {**outcome, **resolution}
    finally:
        close = getattr(redis_client, "close", None)
        if callable(close):
            close()


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
    probe_runner: Callable[..., dict] = run_observe_probe,
    healing_runner: Callable[..., dict] = run_healing_probe,
    healing_runtime_factory: Callable[..., object] = (
        _healing_runtime_factory
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
    browser = settings.get("browser", {})
    elements = (
        browser.get("action_elements", {})
        if isinstance(browser, dict)
        else {}
    )
    elements = normalize_element_definitions(elements)
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
        disabled_outcome: dict[str, int] = {}
        selected_clock = clock or SystemClock()
        clock_now = getattr(selected_clock, "now", None)
        maintenance_now = (
            clock_now() if callable(clock_now) else SystemClock().now()
        )
        if callable(
            getattr(
                store_factory,
                "abort_disabled_element_publications",
                None,
            )
        ):
            disabled_outcome = _settle_disabled_element_publications(
                store_factory=store_factory,
                db_path=selected_db_path,
                redis_factory=redis_factory,
                registry_factory=registry_factory,
                redis_url=selected_redis_url,
                environment=config.environment,
                site=config.site,
                error_code="probe_disabled",
                now=maintenance_now,
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
            **disabled_outcome,
        }
    if config.observe_only and callable(
        getattr(
            store_factory,
            "abort_disabled_element_publications",
            None,
        )
    ):
        with store_factory(selected_db_path) as store:
            selected_clock = clock or SystemClock()
            clock_now = getattr(selected_clock, "now", None)
            preflight_now = (
                clock_now() if callable(clock_now) else SystemClock().now()
            )
            if not isinstance(preflight_now, datetime):
                raise ValueError("clock.now() must return a datetime")
            abort = getattr(
                store,
                "abort_disabled_element_publications",
                None,
            )
            outcome = (
                _settle_disabled_element_publications(
                    store_factory=store_factory,
                    db_path=selected_db_path,
                    redis_factory=redis_factory,
                    registry_factory=registry_factory,
                    redis_url=selected_redis_url,
                    environment=config.environment,
                    site=config.site,
                    error_code="rollout_disabled",
                    now=preflight_now,
                )
                if callable(abort)
                else {
                    "aborted": 0,
                    "inflight": 0,
                    "indeterminate": 0,
                }
            )
        if (
            outcome["aborted"]
            or outcome["inflight"]
            or outcome["indeterminate"]
        ):
            return {
                "status": "rollout_disabled",
                "observe_only": True,
                **outcome,
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
            seeder = getattr(store, "seed_legacy_elements", None)
            if callable(seeder):
                seeder(elements, default_tiktok_contracts())
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
            if config.observe_only:
                adspower_client = adspower_factory(
                    base_url=adspower.get("base_url"),
                    api_key=adspower.get("api_key"),
                    timeout=5.0,
                    max_retries=1,
                )
                return probe_runner(
                    config=config,
                    store=store,
                    redis_client=redis_client,
                    adspower_client=adspower_client,
                    clock=selected_clock,
                    elements=elements,
                    stop_event=stop_event,
                    force=force,
                    management_request_id=management_request_id,
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
                return {"status": "lease_busy", "observe_only": False}
            heartbeat = _LeaseHeartbeat(lease)
            heartbeat.start()
            run_id: int | None = None
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
                reconcile_runner(store, registry)
                now = selected_clock.now()
                if not isinstance(now, datetime):
                    raise ValueError("clock.now() must return a datetime")
                slot = due_daily_slot(
                    now,
                    _last_terminal_slot(store),
                    config.timezone,
                    config.daily_time,
                )
                if slot is None and not force:
                    return {"status": "not_due", "observe_only": False}
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
                            "observe_only": False,
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
                adspower_client = adspower_factory(
                    base_url=adspower.get("base_url"),
                    api_key=adspower.get("api_key"),
                    timeout=5.0,
                    max_retries=1,
                )
                runtime = healing_runtime_factory(
                    config=config,
                    settings=settings,
                    store=store,
                    registry=registry,
                    adspower_client=adspower_client,
                    elements=elements,
                    stop_event=stop_event,
                    lease_guard=heartbeat.require_owned,
                    probe_run_id=run_id,
                    attempt_token=attempt_token,
                )
                with runtime as opened_runtime:
                    result = healing_runner(opened_runtime)
                    if not isinstance(result, dict):
                        raise RuntimeError("healing result must be an object")
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
                heartbeat.require_owned(renew=True)
                status = str(result.get("status") or "probe_unavailable")
                recovery: tuple[
                    dict[str, object],
                    tuple[str, ...],
                    str,
                ] | None = None
                recovery_pending = getattr(store, "recovery_pending", None)
                if (
                    status in {"healthy", "published"}
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
                }
                next_retry_count = retry_count + 1 if infrastructure else 0
                policy_outcome = (
                    "selector_failure"
                    if status == "selector_validation_failed"
                    else (
                        "validated"
                        if status in {"healthy", "published"}
                        else "infrastructure"
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
                if status == "selector_validation_failed" and failed_aliases:
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
                            "match_count": result.get("match_count"),
                            "required_state": str(
                                result.get("required_state") or ""
                            ),
                            "screenshot_path": str(
                                result.get("screenshot_path") or ""
                            ),
                            "occurred_at": now.astimezone(UTC).isoformat(),
                        },
                    }
                elif infrastructure:
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
                        "observe_only": False,
                        "status": status,
                        "published": result.get("published") is True,
                        "failure_code": result.get("failure_code", ""),
                        "match_count": result.get("match_count"),
                        "required_state": result.get("required_state", ""),
                        "validation_records": validation_records,
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
                    policy=_probe_policy(
                        config,
                        outcome=policy_outcome,
                        now=now,
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
                return result
            except BaseException as error:
                if run_id is not None:
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
                            "observe_only": False,
                            "failure_code": _error_code(error),
                            "retry_count": next_retry_count,
                            "retry_at": _retry_at(
                                selected_clock.now(),
                                next_retry_count,
                            ).isoformat(),
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
                if tick_runner is run_tick:
                    consume_element_requests()
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
