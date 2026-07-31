"""Sanitized selector-probe Registry API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from browser_public_identity import mask_profile_id
from gateway.auth_blueprint import allow_roles
from selector_probe.catalog import ElementCatalog, ElementQuery
from selector_probe.gates import StrategyGateService
from selector_probe.discovery import merge_discovery_candidates
from selector_probe.registry import RedisSelectorRegistry
from selector_probe.store import (
    ElementAlreadyExistsError,
    ElementHasDependenciesError,
    ElementMigrationConflictError,
    ElementNotFoundError,
    ElementRequestInProgressError,
    GateStillActiveError,
    ManagementIdempotencyConflictError,
    SelectorProbeStore,
    StaleElementRevisionError,
    StaleManagementRevisionError,
)
from selector_probe.redaction import (
    DEFAULT_EVIDENCE_ROOT,
    redact_evidence,
    resolve_evidence_path,
)
from selector_probe.view_models import (
    public_element_detail,
    public_element_request,
    public_element_summary,
)


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
MANAGEMENT_PAGE_SIZES = frozenset({20, 50, 100})
PREFLIGHT_TOKEN_MAX_AGE_SECONDS = 600
RUN_NOW_TTL_SECONDS = 900
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_RUN_NOW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
""".strip()
RENEW_RUN_NOW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
""".strip()


def default_store_factory() -> SelectorProbeStore:
    path = Path(
        os.getenv("SELECTOR_PROBE_DB_PATH", "").strip()
        or PROJECT_ROOT / "data" / "selector-probe.db"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return SelectorProbeStore(path)


def default_registry_factory() -> RedisSelectorRegistry:
    from gateway.settings_store import load_settings
    from redis import Redis
    from selector_probe.config import normalize_probe_config

    settings = load_settings()
    probe_settings = settings.get("selector_probe")
    config = normalize_probe_config(probe_settings)
    redis_config = (
        probe_settings.get("redis", {})
        if isinstance(probe_settings, Mapping)
        else {}
    )
    redis_config = (
        redis_config if isinstance(redis_config, Mapping) else {}
    )
    client = Redis.from_url(
        str(redis_config.get("url") or "").strip()
        or os.getenv("CELERY_BROKER_URL", "").strip()
        or "redis://127.0.0.1:6379/0",
        password=str(redis_config.get("password") or "") or None,
        socket_connect_timeout=3.0,
        socket_timeout=5.0,
    )
    return RedisSelectorRegistry(
        client,
        environment=config.environment,
        site=config.site,
        namespace=str(
            redis_config.get("namespace") or "selector_registry"
        ),
    )


def default_legacy_elements_provider() -> object:
    from gateway.settings_store import load_settings

    settings = load_settings()
    browser = settings.get("browser", {})
    if not isinstance(browser, Mapping):
        return {}
    return browser.get("action_elements", {})


def default_status_config_provider() -> object:
    from gateway.settings_store import load_settings
    from selector_probe.config import normalize_probe_config

    return normalize_probe_config(load_settings().get("selector_probe"))


def default_settings_provider() -> dict[str, object]:
    from gateway.settings_store import load_settings

    return load_settings()


def default_settings_mutator(mutator) -> dict[str, object]:
    from gateway.settings_store import mutate_settings

    return mutate_settings(mutator)


def default_webhook_test_dispatcher(payload: Mapping[str, object]) -> object:
    from gateway.settings_store import load_settings
    from selector_probe.webhook import WebhookDispatcher

    settings = load_settings()
    probe = settings.get("selector_probe", {})
    probe = probe if isinstance(probe, Mapping) else {}
    webhook = probe.get("webhook", {})
    webhook = webhook if isinstance(webhook, Mapping) else {}
    url = _request_text(
        webhook.get("url"), "webhook_url", maximum=2000
    )
    delivery_id = f"synthetic-{uuid.uuid4().hex}"
    WebhookDispatcher(
        url=url,
        signing_secret=str(webhook.get("signing_secret") or ""),
        webhook_type=str(webhook.get("type") or "generic"),
    ).send(
        dict(payload),
        idempotency_key=delivery_id,
    )
    return {
        "status": "delivered",
        "delivery_id": delivery_id,
    }


def default_settings_preflight_runner(
    raw_settings: Mapping[str, object],
    candidate: Mapping[str, object],
) -> dict[str, str]:
    import requests
    from redis import Redis
    from selector_probe.webhook import WebhookDispatcher

    checks = {
        "profiles": "failed",
        "redis_aof": "failed",
        "redis_eviction": "failed",
        "model": "failed",
        "webhook": "failed",
    }
    probe = raw_settings.get("selector_probe", {})
    probe = probe if isinstance(probe, Mapping) else {}
    profile_ids = [
        item
        for item in probe.get("test_profile_ids", [])
        if isinstance(item, str) and item
    ]
    dedicated_ids = {
        item
        for item in probe.get("dedicated_test_profile_ids", [])
        if isinstance(item, str) and item
    }
    adspower = raw_settings.get("adspower", {})
    adspower = adspower if isinstance(adspower, Mapping) else {}
    try:
        from adspower import AdsPowerController

        base_url = str(
            adspower.get("base_url")
            or "http://local.adspower.net:50325"
        ).rstrip("/")
        api_key = str(adspower.get("api_key") or "")
        structurally_valid = (
            len(profile_ids) >= 2
            and len(set(profile_ids)) == len(profile_ids)
            and dedicated_ids == set(profile_ids)
        )
        if not structurally_valid:
            raise ValueError("dedicated profiles are invalid")
        controller = AdsPowerController(
            base_url=base_url,
            api_key=api_key,
            timeout=5,
            max_retries=1,
            retry_delay=0,
        )
        started_profiles: list[str] = []
        try:
            for profile_id in profile_ids:
                # Track before starting so a partial AdsPower launch is still
                # closed when the API raises before returning a CDP endpoint.
                started_profiles.append(profile_id)
                websocket_url = controller.start_browser(profile_id)
                if not isinstance(websocket_url, str) or not websocket_url:
                    raise RuntimeError("profile CDP is unavailable")
            checks["profiles"] = "passed"
        finally:
            for profile_id in reversed(started_profiles):
                try:
                    controller.stop_browser(profile_id)
                except Exception:
                    checks["profiles"] = "failed"
    except Exception:
        pass
    redis_config = probe.get("redis", {})
    redis_config = (
        redis_config if isinstance(redis_config, Mapping) else {}
    )
    redis_url = str(
        redis_config.get("url")
        or os.getenv("CELERY_BROKER_URL", "")
        or "redis://127.0.0.1:6379/0"
    )
    redis_client = None
    try:
        redis_client = Redis.from_url(
            redis_url,
            password=str(redis_config.get("password") or "") or None,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        if redis_client.ping() is True:
            persistence = redis_client.info("persistence")
            aof_enabled = persistence.get("aof_enabled")
            if aof_enabled in {1, "1", True}:
                checks["redis_aof"] = "passed"
            policy = redis_client.config_get("maxmemory-policy").get(
                "maxmemory-policy"
            )
            if policy == "noeviction":
                checks["redis_eviction"] = "passed"
    except Exception:
        pass
    finally:
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:
                pass
    models = raw_settings.get("models", {})
    models = models if isinstance(models, Mapping) else {}
    model_id = str(probe.get("model_id") or "")
    model = next(
        (
            item
            for item in models.get("items", [])
            if isinstance(item, Mapping)
            and item.get("id") == model_id
            and item.get("enabled", True) is True
        ),
        None,
    )
    try:
        if not isinstance(model, Mapping):
            raise ValueError("model is unavailable")
        base_url = _request_text(
            model.get("base_url"), "model_base_url", maximum=1000
        ).rstrip("/")
        api_key = _request_text(
            model.get("api_key"), "model_api_key", maximum=4000
        )
        response = requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        response.raise_for_status()
        checks["model"] = "passed"
    except Exception:
        pass
    webhook = probe.get("webhook", {})
    webhook = webhook if isinstance(webhook, Mapping) else {}
    if webhook.get("enabled") is True:
        try:
            webhook_url = _request_text(
                webhook.get("url"), "webhook_url", maximum=2000
            )
            WebhookDispatcher(
                url=webhook_url,
                signing_secret=str(
                    webhook.get("signing_secret") or ""
                ),
                webhook_type=str(webhook.get("type") or "generic"),
            ).send(
                {
                    "event": "selector_probe.webhook_test",
                    "environment": candidate.get("environment"),
                    "site": candidate.get("site"),
                    "synthetic": True,
                },
                idempotency_key=(
                    "selector-probe-preflight-"
                    + _settings_candidate_fingerprint(candidate)[:32]
                ),
            )
            checks["webhook"] = "passed"
        except Exception:
            pass
    return checks


def default_element_request_dispatcher(
    request_id: str,
    *,
    store_factory=default_store_factory,
) -> object:
    from selector_probe.worker import wake_element_request_worker

    return wake_element_request_worker(
        request_id,
        store_factory=lambda _path: store_factory(),
    )


def _close_resources_independently(resources: Sequence[object]) -> None:
    first_error: BaseException | None = None
    closed: set[int] = set()
    for resource in resources:
        if resource is None or id(resource) in closed:
            continue
        closed.add(id(resource))
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def default_gate_service_factory(
    *,
    store_factory=default_store_factory,
    redis_factory=None,
) -> StrategyGateService:
    from gateway.settings_store import load_settings
    from selector_probe.config import normalize_probe_config

    config = normalize_probe_config(load_settings().get("selector_probe"))
    store = store_factory()
    redis_client = None
    try:
        redis_client = (redis_factory or _dispatcher_redis_client)()
        return StrategyGateService(
            store,
            redis_client=redis_client,
            environment=config.environment,
            site=config.site,
        )
    except BaseException:
        try:
            _close_resources_independently((redis_client, store))
        except BaseException:
            pass
        raise


def unavailable_run_dispatcher(_request_id: str, _done: object) -> object:
    raise RuntimeError("selector probe dispatcher is unavailable")


def _dispatcher_redis_client() -> object:
    from gateway.settings_store import load_settings
    from redis import Redis

    settings = load_settings()
    probe = settings.get("selector_probe", {})
    probe = probe if isinstance(probe, Mapping) else {}
    redis_config = probe.get("redis", {})
    redis_config = (
        redis_config if isinstance(redis_config, Mapping) else {}
    )
    return Redis.from_url(
        str(redis_config.get("url") or "").strip()
        or os.getenv("CELERY_BROKER_URL", "").strip()
        or "redis://127.0.0.1:6379/0",
        password=str(redis_config.get("password") or "") or None,
        socket_connect_timeout=3.0,
        socket_timeout=5.0,
    )


class RedisRunDispatcher:
    def __init__(
        self,
        *,
        redis_factory,
        tick_runner,
        environment: str,
        site: str,
        ttl_seconds: int = RUN_NOW_TTL_SECONDS,
        heartbeat_seconds: float | None = None,
        namespace: str = "selector_registry",
        terminal_callback=None,
    ) -> None:
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or ttl_seconds < 30
            or ttl_seconds > 86_400
        ):
            raise ValueError("run-now TTL must be between 30 and 86400")
        self.redis_factory = redis_factory
        self.tick_runner = tick_runner
        safe_namespace = str(namespace or "").strip()
        if (
            not safe_namespace
            or len(safe_namespace) > 32
            or not all(
                character.isalnum() or character in "_-"
                for character in safe_namespace
            )
        ):
            raise ValueError("run-now namespace is invalid")
        self.key = (
            f"{safe_namespace}:{environment}:{site}:run_now"
        )
        self.ttl_seconds = ttl_seconds
        interval = heartbeat_seconds or min(30.0, ttl_seconds / 3)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or interval <= 0
            or interval >= ttl_seconds
        ):
            raise ValueError("run-now heartbeat must be below TTL")
        self.heartbeat_seconds = float(interval)
        self.terminal_callback = terminal_callback

    def _release(self, redis_client: object, owner: str) -> None:
        try:
            redis_client.eval(
                RELEASE_RUN_NOW_LUA,
                1,
                self.key,
                owner,
            )
        except Exception:
            pass

    def _renew(self, redis_client: object, owner: str) -> bool:
        try:
            return bool(
                redis_client.eval(
                    RENEW_RUN_NOW_LUA,
                    1,
                    self.key,
                    owner,
                    self.ttl_seconds,
                )
            )
        except Exception:
            return False

    def __call__(self, request_id: str, done) -> dict[str, object]:
        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id != request_id.strip()
            or len(request_id) > 128
        ):
            raise ValueError("invalid run request ID")
        redis_client = self.redis_factory()
        try:
            acquired = redis_client.set(
                self.key,
                request_id,
                nx=True,
                ex=self.ttl_seconds,
            )
        except Exception:
            close = getattr(redis_client, "close", None)
            if callable(close):
                close()
            raise
        if acquired is not True:
            active_run_id = ""
            try:
                raw_owner = redis_client.get(self.key)
                owner = (
                    raw_owner.decode("utf-8")
                    if isinstance(raw_owner, bytes)
                    else str(raw_owner or "")
                )
                if (
                    owner
                    and owner == owner.strip()
                    and len(owner) <= 128
                    and all(
                        character.isalnum()
                        or character in "._:-"
                        for character in owner
                    )
                ):
                    active_run_id = owner
            except Exception:
                active_run_id = ""
            close = getattr(redis_client, "close", None)
            if callable(close):
                close()
            result = {"status": "busy"}
            if active_run_id:
                result["active_run_id"] = active_run_id
            return result

        def run() -> None:
            heartbeat_stop = threading.Event()
            cancel_event = threading.Event()
            result: object = None
            failure_code = ""

            def heartbeat_run() -> None:
                while not heartbeat_stop.wait(self.heartbeat_seconds):
                    if not self._renew(redis_client, request_id):
                        cancel_event.set()
                        return

            heartbeat = threading.Thread(
                target=heartbeat_run,
                name=f"selector-probe-run-now-heartbeat-{request_id[:12]}",
                daemon=True,
            )
            heartbeat.start()
            try:
                try:
                    parameters = inspect.signature(
                        self.tick_runner
                    ).parameters
                except (TypeError, ValueError):
                    parameters = {}
                kwargs = {"force": True}
                if "stop_event" in parameters:
                    kwargs["stop_event"] = cancel_event
                if "management_request_id" in parameters:
                    kwargs["management_request_id"] = request_id
                result = self.tick_runner(**kwargs)
            except BaseException as error:
                failure_code = _safe_code_text(
                    getattr(error, "code", None),
                    maximum=128,
                ) or "probe_unavailable"
            finally:
                heartbeat_stop.set()
                heartbeat.join(min(self.heartbeat_seconds + 0.5, 2.0))
                if callable(self.terminal_callback):
                    try:
                        self.terminal_callback(
                            request_id,
                            result=result,
                            failure_code=failure_code,
                        )
                    except Exception:
                        pass
                try:
                    self._release(redis_client, request_id)
                finally:
                    try:
                        close = getattr(redis_client, "close", None)
                        if callable(close):
                            close()
                    except Exception:
                        pass
                    finally:
                        try:
                            done()
                        except Exception:
                            pass

        thread = threading.Thread(
            target=run,
            name=f"selector-probe-run-now-{request_id[:12]}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            self._release(redis_client, request_id)
            close = getattr(redis_client, "close", None)
            if callable(close):
                close()
            raise
        return {
            "status": "accepted",
            "completion_managed": True,
        }


def default_run_dispatcher(request_id: str, done) -> object:
    from gateway.settings_store import load_settings
    from selector_probe.config import normalize_probe_config
    from selector_probe.worker import run_tick

    settings = load_settings()
    config = normalize_probe_config(settings.get("selector_probe"))
    probe = settings.get("selector_probe", {})
    probe = probe if isinstance(probe, Mapping) else {}
    redis_config = probe.get("redis", {})
    redis_config = (
        redis_config if isinstance(redis_config, Mapping) else {}
    )

    def finish_request(
        selected_request_id: str,
        *,
        result: object,
        failure_code: str,
    ) -> None:
        result_status = (
            str(result.get("status") or "")
            if isinstance(result, Mapping)
            else ""
        )
        success = result_status in {"completed", "healthy", "published"}
        with default_store_factory() as store:
            store.finish_management_run_request(
                selected_request_id,
                status="completed" if success else "failed",
                failure_code=(
                    ""
                    if success
                    else failure_code or result_status or "probe_unavailable"
                ),
            )

    dispatcher = RedisRunDispatcher(
        redis_factory=_dispatcher_redis_client,
        tick_runner=run_tick,
        environment=config.environment,
        site=config.site,
        namespace=str(
            redis_config.get("namespace") or "selector_registry"
        ),
        terminal_callback=finish_request,
    )
    return dispatcher(request_id, done)


@contextmanager
def _open_store(factory):
    resource = factory()
    enter = getattr(resource, "__enter__", None)
    exit_method = getattr(resource, "__exit__", None)
    if callable(enter) and callable(exit_method):
        with resource as store:
            yield store
        return
    try:
        yield resource
    finally:
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def _close_registry(registry: object) -> None:
    close = getattr(registry, "close", None)
    if callable(close):
        close()
        return
    redis_client = getattr(registry, "redis", None)
    close = getattr(redis_client, "close", None)
    if callable(close):
        close()


def _close_gate_service(service: object) -> None:
    close = getattr(service, "close", None)
    if callable(close):
        close()
        return
    redis_client = getattr(service, "redis", None)
    store = getattr(service, "store", None)
    _close_resources_independently((redis_client, store))


@contextmanager
def _open_gate_service(factory):
    service = factory()
    enter = getattr(service, "__enter__", None)
    exit_method = getattr(service, "__exit__", None)
    if callable(enter) and callable(exit_method):
        with service as opened:
            yield opened
        return
    try:
        yield service
    finally:
        _close_gate_service(service)


def _public_gate_reason(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    source = value.get("source")
    reason_code = value.get("reason_code")
    if source not in {"manual", "probe"}:
        return None
    if (
        not isinstance(reason_code, str)
        or not reason_code
        or len(reason_code) > 64
        or not reason_code.replace("_", "").isalnum()
    ):
        return None
    aliases = value.get("aliases")
    if not isinstance(aliases, Sequence) or isinstance(
        aliases,
        (str, bytes, bytearray),
    ):
        aliases = []
    result: dict[str, object] = {
        "source": source,
        "reason_code": reason_code,
        "aliases": [
            safe
            for alias in aliases
            if (safe := _safe_code_text(alias)) is not None
        ][:256],
    }
    selector_version_id = _safe_code_text(value.get("selector_version_id"))
    if selector_version_id is not None:
        result["selector_version_id"] = selector_version_id
    created_at = _safe_timestamp(value.get("created_at"))
    if created_at is not None:
        result["created_at"] = created_at
    return result


def _public_gate_decision(
    value: object,
    *,
    strategy_id: str,
) -> dict[str, object]:
    if hasattr(value, "public_dict"):
        value = value.public_dict()
    if not isinstance(value, Mapping):
        value = {}
    allowed = value.get("allowed")
    reasons = value.get("reasons")
    projected_reasons = (
        [
            public
            for item in reasons
            if (public := _public_gate_reason(item)) is not None
        ]
        if isinstance(reasons, Sequence)
        and not isinstance(reasons, (str, bytes, bytearray))
        else []
    )
    if allowed is not True and not projected_reasons:
        projected_reasons = [
            {
                "source": "probe",
                "reason_code": "registry_unavailable",
                "aliases": [],
                "selector_version_id": "",
            }
        ]
    is_allowed = allowed is True
    return {
        "strategy_id": strategy_id,
        "allowed": is_allowed,
        "effective_status": "active" if is_allowed else "paused",
        "reasons": projected_reasons,
    }


def check_strategy_gate(factory, strategy_id: str) -> dict[str, object]:
    try:
        with _open_gate_service(factory) as service:
            return _public_gate_decision(
                service.check(strategy_id),
                strategy_id=strategy_id,
            )
    except Exception:
        return _public_gate_decision({}, strategy_id=strategy_id)


def _gate_strategy_ids(service: object) -> tuple[str, ...]:
    store = getattr(service, "store", service)
    method = getattr(store, "managed_strategy_ids", None)
    strategy_ids = set(method() if callable(method) else ())
    connection = getattr(store, "connection", None)
    if connection is not None:
        site = getattr(service, "site", "tiktok")
        environment = getattr(service, "environment", "production")
        rows = connection.execute(
            """
            SELECT strategy_id FROM strategy_dependencies
            UNION
            SELECT strategy_id FROM strategy_gate_reasons
            WHERE cleared_at IS NULL
              AND (
                  source = 'manual'
                  OR (source = 'probe' AND site = ? AND environment = ?)
              )
            ORDER BY strategy_id
            """,
            (site, environment),
        ).fetchall()
        strategy_ids.update(str(row["strategy_id"]) for row in rows)
    return tuple(
        sorted(
            strategy_id
            for strategy_id in strategy_ids
            if isinstance(strategy_id, str) and strategy_id
        )
    )


def _pagination() -> tuple[int, int]:
    raw_limit = request.args.get("limit", str(DEFAULT_PAGE_LIMIT))
    raw_offset = request.args.get("offset", "0")
    try:
        limit = int(raw_limit)
        offset = int(raw_offset)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_pagination") from error
    if limit < 1 or offset < 0:
        raise ValueError("invalid_pagination")
    return min(limit, MAX_PAGE_LIMIT), offset


def _management_pagination() -> tuple[int, int]:
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "20"))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_pagination") from error
    if page < 1 or page_size not in MANAGEMENT_PAGE_SIZES:
        raise ValueError("invalid_pagination")
    return page, page_size


def _management_actor() -> tuple[int, str]:
    user = getattr(g, "management_user", None)
    if user is None:
        return 1, "operator"
    return int(user.id), str(user.username)


def _strict_object(
    value: object,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or not required.issubset(value)
        or not set(value).issubset(required | optional)
    ):
        raise ValueError("invalid_request")
    return value


def _request_text(
    value: object,
    name: str,
    *,
    maximum: int = 500,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
    ):
        raise ValueError(f"invalid_{name}")
    return value


def _request_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid_expected_revision")
    return value


def _safe_json_mapping(value: object) -> dict[str, object]:
    decoded = _json_value(value, {})
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _safe_scalar(value: object, *, maximum: int = 128) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value[:maximum]
    return None


def _safe_code_text(
    value: object,
    *,
    maximum: int = 128,
) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        return None
    if not value[0].isalnum() or not all(
        character.isalnum() or character in "._:-"
        for character in value
    ):
        return None
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "access_key",
            "access_token",
            "api_key",
            "apikey",
            "authorization",
            "bearer",
            "client_secret",
            "cookie",
            "credential",
            "password",
            "private_key",
            "secret",
            "token",
        )
    ):
        return None
    if lowered.startswith(("ghp_", "github_pat_", "sk-", "pk-")):
        return None
    if len(value) >= 40 and value.count(".") == 2:
        return None
    return value


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return value


def _safe_projected_value(key: str, value: object) -> object:
    if key in {
        "attempt",
        "attempt_count",
        "match_count",
        "occurrence_count",
        "repair_attempt_count",
        "revision",
        "round",
        "version_id",
    }:
        if (
            not isinstance(value, bool)
            and isinstance(value, int)
            and value >= 0
        ):
            return value
        return None
    if key in {"published", "reconciled", "synthetic"}:
        return value if isinstance(value, bool) else None
    if key in {
        "created_at",
        "finished_at",
        "first_seen_at",
        "last_seen_at",
        "next_attempt_at",
        "next_retry_at",
        "occurred_at",
        "published_at",
        "resolved_at",
        "scheduled_for",
        "started_at",
        "validated_at",
        "acknowledged_at",
    }:
        return _safe_timestamp(value)
    if key == "profile_mask":
        if (
            isinstance(value, str)
            and value.startswith("***")
            and 3 <= len(value) <= 16
            and not any(ord(character) < 32 for character in value)
        ):
            return value
        return None
    return _safe_code_text(value)


def _safe_username(value: object) -> str:
    selected = _safe_code_text(value, maximum=64)
    return selected if selected is not None else "system"


def _safe_reason_text(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or any(ord(character) < 32 for character in value)
    ):
        return None
    lowered = value.casefold()
    private_markers = (
        "://",
        "api_key",
        "apikey",
        "authorization",
        "bearer ",
        "cookie",
        "password",
        "secret",
        "token",
    )
    if any(marker in lowered for marker in private_markers):
        return None
    return value


def _project_records(
    value: object,
    allowed: Sequence[str],
    *,
    limit: int = 200,
) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return []
    result = []
    for item in value[:limit]:
        if not isinstance(item, Mapping):
            continue
        projected = {
            key: safe
            for key in allowed
            if (
                safe := _safe_projected_value(key, item.get(key))
            ) is not None
        }
        result.append(projected)
    return result


def _safe_operation_state(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    allowed = (
        "status",
        "code",
        "failure_code",
        "attempt_count",
        "version_id",
        "started_at",
        "finished_at",
        "next_attempt_at",
        "reconciled",
        "published",
    )
    return {
        key: safe
        for key in allowed
        if (safe := _safe_projected_value(key, value.get(key))) is not None
    }


def _safe_audit_details(value: object) -> dict[str, object]:
    raw = _safe_json_mapping(value)
    result: dict[str, object] = {}
    reason = _safe_reason_text(raw.get("reason"))
    if reason is not None:
        result["reason"] = reason
    for key in (
        "error_code",
        "request_id",
        "draft_version",
        "retry_of_run_id",
    ):
        safe = _safe_code_text(raw.get(key))
        if safe is not None:
            result[key] = safe
    attempt_count = raw.get("attempt_count")
    if (
        not isinstance(attempt_count, bool)
        and isinstance(attempt_count, int)
        and attempt_count >= 0
    ):
        result["attempt_count"] = attempt_count
    if isinstance(raw.get("synthetic"), bool):
        result["synthetic"] = raw["synthetic"]
    changes = raw.get("dangerous_changes")
    if isinstance(changes, Sequence) and not isinstance(
        changes, (str, bytes, bytearray)
    ):
        result["dangerous_changes"] = [
            safe
            for item in changes[:20]
            if (safe := _safe_code_text(item, maximum=64)) is not None
        ]
    return result


def _management_project_run(value: object) -> dict[str, object]:
    result = _public_run(value)
    if not isinstance(value, Mapping):
        return result
    details = _safe_json_mapping(value.get("details", value.get("details_json")))
    trigger = details.get("trigger")
    if trigger not in {"scheduled", "manual", "retry"}:
        trigger = "scheduled"
    rollout_mode = details.get("rollout_mode")
    if rollout_mode not in {"observe", "publish", "enforce"}:
        rollout_mode = "observe"
    result.update(
        {
            "trigger": trigger,
            "actor": _safe_username(details.get("actor")),
            "due_slot": (
                _safe_timestamp(
                    details.get("due_slot")
                    or value.get("scheduled_for")
                )
                or ""
            ),
            "rollout_mode": rollout_mode,
            "profiles": [
                mask_profile_id(item)
                for item in details.get("profile_ids", [])
                if isinstance(item, str)
            ][:20],
            "stages": _project_records(
                details.get("stages"),
                (
                    "name",
                    "status",
                    "failure_code",
                    "profile_mask",
                    "attempt_count",
                    "round",
                    "summary",
                    "started_at",
                    "finished_at",
                ),
            ),
            "elements": _project_records(
                details.get("elements"),
                (
                    "alias",
                    "element_id",
                    "status",
                    "failure_class",
                    "repair_attempt_count",
                ),
            ),
            "rounds": _project_records(
                details.get("rounds"),
                (
                    "profile_mask",
                    "round",
                    "status",
                    "match_count",
                    "failure_code",
                ),
            ),
            "repairs": _project_records(
                details.get("repairs"),
                (
                    "attempt",
                    "previous_method",
                    "new_method",
                    "failure_code",
                    "match_count",
                    "validation_result",
                    "prompt_version",
                    "model_id",
                ),
            ),
            "publication": _safe_operation_state(
                details.get("publication")
            ),
            "reconciliation": _safe_operation_state(
                details.get("reconciliation")
            ),
            "cleanup": _safe_operation_state(details.get("cleanup")),
            "lease": _safe_operation_state(details.get("lease")),
            "failure": _safe_operation_state(details.get("failure")),
            "retry_of_run_id": _safe_code_text(
                details.get("retry_of_run_id")
            ),
            "next_retry_at": _safe_timestamp(
                details.get("next_retry_at")
            ),
        }
    )
    validations = value.get("validations")
    if isinstance(validations, Sequence) and not isinstance(
        validations, (str, bytes, bytearray)
    ):
        result["rounds"] = _project_records(
            [
                {
                    "profile_mask": item.get("profile_mask"),
                    "round": item.get("round_number"),
                    "status": item.get("result"),
                    "page_state": item.get("page_state"),
                    "failure_code": item.get("failure_code"),
                    "started_at": item.get("started_at"),
                    "finished_at": item.get("finished_at"),
                }
                for item in validations
                if isinstance(item, Mapping)
            ],
            (
                "profile_mask",
                "round",
                "status",
                "page_state",
                "failure_code",
                "started_at",
                "finished_at",
            ),
        )
        result["discoveries"] = merge_discovery_candidates(
            [item for item in validations if isinstance(item, Mapping)]
        )
    else:
        result["discoveries"] = []
    return result


def _management_project_version(value: object) -> dict[str, object]:
    result = _public_version(value)
    if not isinstance(value, Mapping):
        return result
    evidence = _safe_json_mapping(value.get("evidence", value.get("evidence_json")))
    result.update(
        {
            "is_active": value.get("status") == "published",
            "is_lkg": bool(evidence.get("is_lkg")),
            "diff": _safe_operation_state(evidence.get("diff")),
            "dependencies": _project_records(
                evidence.get("dependencies"),
                ("alias", "strategy_id", "action_id", "action_type"),
            ),
            "evidence": redact_evidence(
                {
                    "profiles": _project_records(
                        evidence.get("profiles"),
                        ("profile_mask", "status", "failure_code"),
                        limit=20,
                    ),
                    "rounds": _project_records(
                        evidence.get("rounds"),
                        (
                            "profile_mask",
                            "round",
                            "status",
                            "match_count",
                            "failure_code",
                        ),
                        limit=40,
                    ),
                    "repairs": _project_records(
                        evidence.get("repairs"),
                        (
                            "attempt",
                            "failure_code",
                            "validation_result",
                            "model_id",
                            "prompt_version",
                        ),
                        limit=20,
                    ),
                }
            ),
            "outbox": _safe_operation_state(value.get("outbox")),
            "revision": value.get("revision", 0),
        }
    )
    return result


def _management_project_alert(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    details = _safe_json_mapping(value.get("details", value.get("details_json")))
    aliases = _strings(value.get("aliases", value.get("aliases_json")))
    strategy_ids = _strings(
        value.get("strategy_ids", value.get("strategy_ids_json"))
    )
    severity = details.get("severity")
    if severity not in {"info", "warning", "critical"}:
        severity = "critical"
    return {
        "id": _safe_projected_value("element_id", value.get("id")),
        "status": _safe_projected_value("status", value.get("status")),
        "severity": severity,
        "failure_class": _safe_projected_value(
            "failure_class", value.get("failure_class")
        ),
        "occurrence_count": (
            _safe_projected_value(
                "occurrence_count", value.get("occurrence_count", 0)
            )
            or 0
        ),
        "aliases": aliases,
        "strategy_ids": strategy_ids,
        "active_version": (
            _safe_code_text(value.get("active_version")) or ""
        ),
            "repairs": _project_records(
                details.get("repairs"),
                (
                    "attempt",
                    "failure_code",
                    "validation_result",
                    "model_id",
                    "prompt_version",
                ),
                limit=20,
            ),
            "retries": _project_records(
                details.get("retries"),
                (
                    "attempt",
                    "status",
                    "failure_code",
                    "started_at",
                    "finished_at",
                ),
                limit=20,
            ),
            "webhook": _safe_operation_state(details.get("webhook")),
        "gate_active": bool(
            value.get(
                "gate_active",
                details.get("gate_active", strategy_ids),
            )
        ),
        "screenshot_available": bool(value.get("screenshot_path")),
        "first_seen_at": _safe_timestamp(value.get("first_seen_at")),
        "last_seen_at": _safe_timestamp(value.get("last_seen_at")),
        "acknowledged_at": _safe_timestamp(
            value.get("acknowledged_at")
        ),
        "resolved_at": _safe_timestamp(value.get("resolved_at")),
        "revision": (
            _safe_projected_value("revision", value.get("revision", 0))
            or 0
        ),
        "timeline": _project_records(
            details.get("timeline"),
            ("type", "status", "failure_code", "occurred_at"),
            limit=100,
        ),
    }


def _management_project_gate(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    reasons = []
    for item in value.get("reasons", []):
        public = _public_gate_reason(item)
        if public is None:
            continue
        public["actor"] = _safe_username(
            item.get("created_by") if isinstance(item, Mapping) else None
        )
        public["affected_action_ids"] = _strings(
            value.get("affected_action_ids", [])
        )
        reasons.append(public)
    return {
        "strategy_id": _safe_code_text(value.get("strategy_id")),
        "strategy_name": (
            _safe_reason_text(value.get("strategy_name")) or ""
        ),
        "effective_status": (
            _safe_code_text(value.get("effective_status")) or "unmanaged"
        ),
        "managed": bool(value.get("managed")),
        "revision": (
            _safe_projected_value("revision", value.get("revision", 0))
            or 0
        ),
        "reasons": reasons,
        "affected_action_ids": _strings(
            value.get("affected_action_ids", [])
        ),
    }


def _settings_secret() -> str:
    value = current_app.config.get("SECRET_KEY")
    return str(value or "selector-probe-local-development-secret")


def _profile_ref(profile_id: str) -> str:
    digest = hmac.new(
        _settings_secret().encode("utf-8"),
        f"selector-probe-profile:{profile_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"prf_{digest}"


def _candidate_fingerprint(candidate: Mapping[str, object]) -> str:
    payload = json.dumps(
        candidate,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _operation_payload_hash(payload: Mapping[str, object]) -> str:
    return "sha256:" + _candidate_fingerprint(payload)


def _settings_candidate_fingerprint(
    candidate: Mapping[str, object],
) -> str:
    profiles = candidate.get("profiles", [])
    payload = {
        key: candidate.get(key)
        for key in (
            "enabled",
            "rollout_mode",
            "schedule_time",
            "timezone",
            "target_origin",
            "freshness_hours",
            "retry_policy",
        )
    }
    payload["profiles"] = [
        {
            "profile_ref": item.get("profile_ref"),
            "dedicated_test": item.get("dedicated_test"),
        }
        for item in profiles
        if isinstance(item, Mapping)
    ]
    model = candidate.get("model", {})
    redis_value = candidate.get("redis", {})
    webhook = candidate.get("webhook", {})
    payload["model"] = {
        "id": model.get("id") if isinstance(model, Mapping) else ""
    }
    payload["redis"] = {
        "namespace": (
            redis_value.get("namespace")
            if isinstance(redis_value, Mapping)
            else ""
        )
    }
    payload["webhook"] = {
        key: webhook.get(key)
        for key in (
            "enabled",
            "type",
            "timeout_seconds",
            "retry_policy",
        )
    } if isinstance(webhook, Mapping) else {}
    return _candidate_fingerprint(payload)


def _settings_publication_fingerprint(
    candidate: Mapping[str, object],
) -> str:
    payload = {
        key: candidate.get(key)
        for key in (
            "enabled",
            "rollout_mode",
            "schedule_time",
            "timezone",
            "target_origin",
            "freshness_hours",
            "site",
            "environment",
            "retry_policy",
            "profiles",
            "model",
            "redis",
            "webhook",
        )
    }
    return "sha256:" + _candidate_fingerprint(payload)


def _settings_private_reference(
    raw_settings: Mapping[str, object],
) -> str:
    private_candidate = {
        key: raw_settings.get(key)
        for key in ("selector_probe", "models", "adspower")
    }
    encoded = json.dumps(
        private_candidate,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hmac.new(
        _settings_secret().encode("utf-8"),
        encoded,
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _safe_webhook_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}/***"


def _selector_settings_projection(
    raw: object,
    *,
    revision: int,
) -> dict[str, object]:
    settings = raw if isinstance(raw, Mapping) else {}
    probe = settings.get("selector_probe", {})
    probe = probe if isinstance(probe, Mapping) else {}
    profile_ids = probe.get("test_profile_ids", [])
    if not isinstance(profile_ids, Sequence) or isinstance(
        profile_ids, (str, bytes, bytearray)
    ):
        profile_ids = []
    dedicated_ids = {
        item
        for item in probe.get("dedicated_test_profile_ids", [])
        if isinstance(item, str) and item
    }
    profile_health = probe.get("profile_health", {})
    profile_health = (
        profile_health if isinstance(profile_health, Mapping) else {}
    )
    models = settings.get("models", {})
    models = models if isinstance(models, Mapping) else {}
    model_id = str(probe.get("model_id") or models.get("default_model_id") or "")
    model = next(
        (
            item
            for item in models.get("items", [])
            if isinstance(item, Mapping) and item.get("id") == model_id
        ),
        {},
    )
    webhook = probe.get("webhook", {})
    webhook = webhook if isinstance(webhook, Mapping) else {}
    redis_config = probe.get("redis", {})
    redis_config = redis_config if isinstance(redis_config, Mapping) else {}
    mode = str(probe.get("rollout_mode") or "")
    if mode not in {"observe", "publish", "enforce"}:
        mode = "observe" if probe.get("observe_only", True) else "publish"
    return {
        "revision": revision,
        "enabled": bool(probe.get("enabled", False)),
        "rollout_mode": mode,
        "schedule_time": str(probe.get("daily_time") or "03:00"),
        "timezone": str(probe.get("timezone") or "Asia/Shanghai"),
        "target_origin": str(probe.get("target_url") or ""),
        "freshness_hours": int(probe.get("freshness_hours") or 36),
        "site": str(probe.get("site") or "tiktok"),
        "environment": str(probe.get("environment") or "production"),
        "retry_policy": probe.get(
            "retry_policy",
            {"attempts": 3, "refresh_between_attempts": True},
        ),
        "profiles": [
            {
                "profile_ref": _profile_ref(item),
                "profile_mask": mask_profile_id(item),
                "dedicated_test": item in dedicated_ids,
                "status": (
                    "healthy"
                    if profile_health.get(item) == "healthy"
                    else "unknown"
                ),
            }
            for item in profile_ids
            if isinstance(item, str) and item
        ],
        "model": {
            "id": model_id,
            "provider": str(model.get("provider") or ""),
            "mode": str(model.get("mode") or ""),
            "status": "passed" if model_id and model.get("enabled", True) else "failed",
            "api_key_set": bool(model.get("api_key")),
        },
        "redis": {
            "status": str(redis_config.get("status") or "unknown"),
            "namespace": str(
                redis_config.get("namespace") or "selector_registry"
            ),
            "aof_enabled": bool(redis_config.get("aof_enabled", False)),
            "eviction_policy": str(redis_config.get("eviction_policy") or ""),
            "password_set": bool(redis_config.get("password")),
        },
        "webhook": {
            "enabled": bool(webhook.get("enabled", False)),
            "type": str(webhook.get("type") or "generic"),
            "url_display": _safe_webhook_url(webhook.get("url")),
            "signing_secret_set": bool(webhook.get("signing_secret")),
            "status": str(webhook.get("status") or "unknown"),
            "timeout_seconds": int(webhook.get("timeout_seconds") or 10),
            "retry_policy": webhook.get(
                "retry_policy", {"attempts": 3}
            ),
        },
    }


def _settings_checks(candidate: Mapping[str, object]) -> dict[str, str]:
    profiles = candidate.get("profiles", [])
    valid_profiles = (
        isinstance(profiles, Sequence)
        and not isinstance(profiles, (str, bytes, bytearray))
        and len(profiles) >= 2
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("profile_ref"), str)
            and item.get("profile_ref")
            and item.get("dedicated_test") is True
            and item.get("status") == "healthy"
            for item in profiles
        )
    )
    redis_value = candidate.get("redis", {})
    redis_value = redis_value if isinstance(redis_value, Mapping) else {}
    model = candidate.get("model", {})
    model = model if isinstance(model, Mapping) else {}
    webhook = candidate.get("webhook", {})
    webhook = webhook if isinstance(webhook, Mapping) else {}
    return {
        "profiles": "passed" if valid_profiles else "failed",
        "redis_aof": "passed" if redis_value.get("aof_enabled") is True else "failed",
        "redis_eviction": (
            "passed"
            if redis_value.get("eviction_policy") == "noeviction"
            else "failed"
        ),
        "model": (
            "passed"
            if model.get("id") and model.get("status") == "passed"
            else "failed"
        ),
        "webhook": (
            "passed"
            if (
                webhook.get("enabled") is True
                and webhook.get("url_display")
                and webhook.get("status") == "passed"
            )
            else "failed"
        ),
    }


def _element_query() -> ElementQuery:
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "20"))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_pagination") from error
    return ElementQuery(
        page=page,
        page_size=page_size,
        search=request.args.get("search", ""),
        status=request.args.get("status", "all"),
        source=request.args.get("source", "all"),
        scope=request.args.get("scope", "all"),
        referenced=request.args.get("referenced", "all"),
    )


def _actor_identity() -> tuple[int, str]:
    return _management_actor()


def _element_detail_payload(
    catalog: ElementCatalog,
    record: object,
    *,
    active_definition: object = None,
) -> dict[str, object]:
    draft = catalog.draft(record.id)
    dependencies = catalog.dependencies(record.id)
    validation = draft["validation"] if draft is not None else {}
    draft_candidates = draft["candidates"] if draft is not None else []
    repairs = (
        validation.get("repairs", [])
        if isinstance(validation, Mapping)
        else []
    )
    active_locators = (
        active_definition.get("locators", [])
        if isinstance(active_definition, Mapping)
        else []
    )
    has_repairs = (
        isinstance(repairs, Sequence)
        and not isinstance(repairs, (str, bytes, bytearray))
        and bool(repairs)
    )
    comparison = {
        "active": active_locators,
        "deterministic": [] if has_repairs else draft_candidates,
        "repaired": draft_candidates if has_repairs else [],
    }
    payload = public_element_detail(
        record,
        validation,
        dependencies,
        candidate_comparison=comparison,
        repairs=repairs,
        history=catalog.history(record.id),
    )
    payload["contract"] = draft["contract"] if draft is not None else None
    payload["candidates"] = (
        [
            projected
            for item in (active_locators or draft_candidates)
            if (projected := _public_locator(item))
        ]
        if active_locators or draft is not None
        else []
    )
    payload["draft_revision"] = (
        draft["revision"] if draft is not None else None
    )
    payload["base_version_id"] = (
        draft["base_version_id"] if draft is not None else ""
    )
    return payload


def _element_error(error: BaseException):
    if isinstance(error, ElementNotFoundError):
        return jsonify({"code": "element_not_found"}), 404
    if isinstance(error, StaleElementRevisionError):
        return jsonify({"code": "stale_revision"}), 409
    if isinstance(error, ElementHasDependenciesError):
        return jsonify({"code": "element_has_dependencies"}), 409
    if isinstance(error, ElementAlreadyExistsError):
        return jsonify({"code": "element_already_exists"}), 409
    if isinstance(error, ElementMigrationConflictError):
        return jsonify({"code": "element_migration_conflict"}), 409
    if isinstance(error, ElementRequestInProgressError):
        return jsonify({"code": "element_request_in_progress"}), 409
    if isinstance(error, ValueError):
        return jsonify({"code": "invalid_element_request"}), 400
    return jsonify({"code": "element_service_unavailable"}), 503


def _json_value(value: object, default: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (RecursionError, TypeError, ValueError):
        return default
    return decoded


def _public_locator(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in (
        "id",
        "type",
        "name",
        "value",
        "role",
        "name_mode",
        "enabled",
        "fallback",
    ):
        item = value.get(key)
        if isinstance(item, (str, bool)):
            result[key] = item
    descendant = value.get("descendant")
    if isinstance(descendant, Mapping):
        projected = _public_locator(descendant)
        if projected:
            result["descendant"] = projected
    return result


def _public_elements(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for alias, definition in value.items():
        if not isinstance(alias, str) or not isinstance(definition, Mapping):
            continue
        scope = definition.get("scope")
        locators = definition.get("locators")
        if (
            not isinstance(scope, str)
            or not isinstance(locators, Sequence)
            or isinstance(locators, (str, bytes, bytearray))
        ):
            continue
        projected = [
            public
            for locator in locators
            if (public := _public_locator(locator))
        ]
        result[alias] = {"scope": scope, "locators": projected}
    return result


def _public_active(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    version = value.get("version")
    elements = _public_elements(value.get("elements"))
    if not isinstance(version, str) or not version or not elements:
        return None
    result: dict[str, object] = {
        "version": version,
        "elements": elements,
    }
    bundle_hash = value.get("bundle_hash")
    if isinstance(bundle_hash, str):
        result["bundle_hash"] = bundle_hash
    return result


def _strings(value: object) -> list[str]:
    decoded = _json_value(value, [])
    if not isinstance(decoded, Sequence) or isinstance(
        decoded,
        (str, bytes, bytearray),
    ):
        return []
    return [
        safe
        for item in decoded
        if (safe := _safe_code_text(item)) is not None
    ][:256]


def _public_run(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    run_id = value.get("id")
    if isinstance(run_id, int) and not isinstance(run_id, bool):
        result["id"] = run_id
    elif (safe_id := _safe_code_text(run_id)) is not None:
        result["id"] = safe_id
    for key in ("scheduled_for", "started_at", "finished_at"):
        item = _safe_timestamp(value.get(key))
        if item is not None:
            result[key] = item
        elif value.get(key) is None and key == "finished_at":
            result[key] = None
    for key in (
        "status",
        "active_version_before",
        "published_version_after",
    ):
        item = _safe_code_text(value.get(key))
        if item is not None:
            result[key] = item
    failed = value.get("failed_aliases", value.get("failed_aliases_json"))
    result["failed_aliases"] = _strings(failed)
    return result


def _public_version(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in (
        "id",
        "site",
        "environment",
        "status",
        "base_version_id",
        "bundle_hash",
        "model_id",
        "prompt_version",
    ):
        item = _safe_code_text(value.get(key))
        if item is not None:
            result[key] = item
    for key in (
        "created_at",
        "validated_at",
        "published_at",
    ):
        item = _safe_timestamp(value.get(key))
        if item is not None or (
            value.get(key) is None and key == "published_at"
        ):
            result[key] = item
    return result


def _sqlite_history(
    store: object,
    kind: str,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, object]]:
    connection = getattr(store, "connection", None)
    if connection is None:
        raise RuntimeError("selector probe history unavailable")
    if kind == "runs":
        rows = connection.execute(
            """
            SELECT id, scheduled_for, started_at, finished_at, status,
                   active_version_before, published_version_after,
                   failed_aliases_json
            FROM probe_runs
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id, site, environment, status, base_version_id,
                   bundle_hash, model_id, prompt_version, created_at,
                   validated_at, published_at
            FROM selector_versions
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def _history(
    store: object,
    kind: str,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, object]]:
    method = getattr(store, f"list_{kind}", None)
    if callable(method):
        value = method(limit=limit, offset=offset)
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes, bytearray),
        ):
            raise RuntimeError("selector probe history unavailable")
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return _sqlite_history(store, kind, limit=limit, offset=offset)


def _safe_status_overview(
    value: object,
    *,
    active: Mapping[str, object] | None,
    latest_run: Mapping[str, object] | None,
    config: object | None,
    now: datetime,
) -> dict[str, object]:
    overview = value if isinstance(value, Mapping) else {}
    raw_health = overview.get("health")
    health_details: dict[str, object] = {}
    if isinstance(raw_health, Mapping):
        status = raw_health.get("status")
        health = status if isinstance(status, str) else "unavailable"
        for field in (
            "failure_started_at",
            "retry_count",
            "next_retry_at",
            "last_validated_at",
        ):
            selected = raw_health.get(field)
            if isinstance(selected, (str, int)) and not isinstance(
                selected,
                bool,
            ):
                health_details[field] = selected
    else:
        health = raw_health if isinstance(raw_health, str) else "unavailable"

    raw_version = overview.get("current_version")
    stored_version = (
        raw_version.get("id")
        if isinstance(raw_version, Mapping)
        else raw_version
    )
    active_version = active.get("version") if active is not None else None
    current_version = (
        active_version
        if isinstance(active_version, str) and active_version
        else (
            stored_version
            if isinstance(stored_version, str) and stored_version
            else ""
        )
    )

    raw_success = overview.get("last_successful")
    last_successful = (
        _public_run(raw_success)
        if isinstance(raw_success, Mapping)
        else None
    )
    if not last_successful:
        last_successful = (
            dict(latest_run)
            if isinstance(latest_run, Mapping)
            and latest_run.get("status") == "completed"
            else None
        )
    last_validated = health_details.get("last_validated_at")
    if not isinstance(last_validated, str) or not last_validated:
        last_validated = (
            last_successful.get("finished_at", "")
            if last_successful is not None
            else ""
        )

    next_run_at = ""
    if config is not None:
        try:
            timezone = ZoneInfo(str(config.timezone))
            local_now = now.astimezone(timezone)
            scheduled = datetime.combine(
                local_now.date(),
                config.daily_time,
                tzinfo=timezone,
            )
            if scheduled <= local_now:
                scheduled += timedelta(days=1)
            next_run_at = scheduled.isoformat()
        except (AttributeError, TypeError, ValueError):
            next_run_at = ""

    count_fields = (
        "all",
        "healthy",
        "using_lkg",
        "draft",
        "queued",
        "probing",
        "validating",
        "failed",
        "probe_unavailable",
        "disabled",
    )
    raw_counts = overview.get("element_counts")
    counts = {
        field: (
            int(raw_counts.get(field, 0))
            if isinstance(raw_counts, Mapping)
            and isinstance(raw_counts.get(field, 0), int)
            and not isinstance(raw_counts.get(field, 0), bool)
            else 0
        )
        for field in count_fields
    }

    priority_elements: list[dict[str, object]] = []
    raw_priority = overview.get("priority_elements")
    if isinstance(raw_priority, Sequence) and not isinstance(
        raw_priority,
        (str, bytes, bytearray),
    ):
        for raw_element in raw_priority[:5]:
            if not isinstance(raw_element, Mapping):
                continue
            element: dict[str, object] = {}
            for field in (
                "id",
                "display_name",
                "management_source",
                "published_status",
                "draft_status",
                "scope",
                "primary_locator_type",
                "last_validated_at",
                "revision",
                "dependency_count",
            ):
                selected = raw_element.get(field)
                if selected is None and field in {
                    "draft_status",
                    "last_validated_at",
                }:
                    element[field] = None
                elif isinstance(selected, (str, int)) and not isinstance(
                    selected,
                    bool,
                ):
                    element[field] = selected
            priority_elements.append(element)

    raw_gates = overview.get("gate_counts")
    gate_counts = {
        field: (
            int(raw_gates.get(field, 0))
            if isinstance(raw_gates, Mapping)
            and isinstance(raw_gates.get(field, 0), int)
            and not isinstance(raw_gates.get(field, 0), bool)
            else 0
        )
        for field in ("automatic", "manual")
    }

    raw_alerts = overview.get("alert_summary")
    alert_summary = {
        field: (
            int(raw_alerts.get(field, 0))
            if isinstance(raw_alerts, Mapping)
            and isinstance(raw_alerts.get(field, 0), int)
            and not isinstance(raw_alerts.get(field, 0), bool)
            else 0
        )
        for field in ("open", "acknowledged", "resolved", "active")
    }
    latest_alert = (
        raw_alerts.get("latest")
        if isinstance(raw_alerts, Mapping)
        else None
    )
    if isinstance(latest_alert, Mapping):
        alert_summary["latest"] = {
            field: selected
            for field in (
                "id",
                "status",
                "failure_class",
                "last_seen_at",
                "occurrence_count",
            )
            if isinstance(
                (selected := latest_alert.get(field)),
                (str, int),
            )
            and not isinstance(selected, bool)
        }

    raw_webhook = overview.get("webhook_status")
    webhook_details: dict[str, object] | None = None
    if isinstance(raw_webhook, Mapping):
        webhook_details = {
            field: selected
            for field in (
                "status",
                "event_type",
                "attempt_count",
                "created_at",
                "completed_at",
            )
            if (
                (selected := raw_webhook.get(field)) is None
                and field == "completed_at"
            )
            or (
                isinstance(selected, (str, int))
                and not isinstance(selected, bool)
            )
        }
    webhook_enabled = (
        getattr(getattr(config, "webhook", None), "enabled", None)
        if config is not None
        else None
    )
    webhook_status = (
        str(webhook_details["status"])
        if webhook_details is not None
        and isinstance(webhook_details.get("status"), str)
        else (
            "configured"
            if webhook_enabled is True
            else "disabled"
            if webhook_enabled is False
            else "unavailable"
        )
    )
    alert_summary["webhook_status"] = webhook_status

    recent_events: list[dict[str, object]] = []
    raw_events = overview.get("recent_events")
    if isinstance(raw_events, Sequence) and not isinstance(
        raw_events,
        (str, bytes, bytearray),
    ):
        for raw_event in raw_events[:10]:
            if not isinstance(raw_event, Mapping):
                continue
            event_type = raw_event.get("event_type")
            target_type = raw_event.get("target_type")
            target_id = raw_event.get("target_id")
            result = raw_event.get("result")
            created_at = raw_event.get("created_at")
            if not all(
                isinstance(item, str)
                for item in (
                    event_type,
                    target_type,
                    target_id,
                    result,
                    created_at,
                )
            ):
                continue
            recent_events.append(
                {
                    "type": event_type,
                    "summary": f"{target_type} {target_id}: {result}"[:500],
                    "occurred_at": created_at,
                }
            )

    raw_revision = overview.get("revision")
    revision = (
        raw_revision
        if isinstance(raw_revision, int) and not isinstance(raw_revision, bool)
        else 0
    )
    return {
        "health": health,
        "health_details": health_details,
        "current_version": current_version,
        "last_successful": last_successful,
        "last_successful_validation_at": last_validated,
        "next_run_at": next_run_at,
        "element_counts": counts,
        "priority_elements": priority_elements,
        "gate_counts": gate_counts,
        "alert_summary": alert_summary,
        "webhook_status": webhook_status,
        "webhook": webhook_details,
        "recent_events": recent_events,
        "revision": revision,
    }


def create_selector_probe_blueprint(
    *,
    store_factory=default_store_factory,
    registry_factory=default_registry_factory,
    gate_service_factory=default_gate_service_factory,
    run_dispatcher=default_run_dispatcher,
    element_request_dispatcher=None,
    legacy_elements_provider=default_legacy_elements_provider,
    status_config_provider=default_status_config_provider,
    settings_provider=default_settings_provider,
    settings_mutator=default_settings_mutator,
    settings_preflight_runner=default_settings_preflight_runner,
    webhook_test_dispatcher=default_webhook_test_dispatcher,
    evidence_root=DEFAULT_EVIDENCE_ROOT,
    utcnow_fn=lambda: datetime.now(UTC),
    monotonic_fn=time.monotonic,
    local_busy_ttl_seconds=RUN_NOW_TTL_SECONDS,
) -> Blueprint:
    blueprint = Blueprint("selector_probe", __name__)
    run_lock = threading.Lock()
    run_owner = ""
    run_busy_until = 0.0
    settings_lock = threading.Lock()
    request_waker = element_request_dispatcher or (
        lambda request_id: default_element_request_dispatcher(
            request_id,
            store_factory=store_factory,
        )
    )

    def active_bundle() -> dict[str, object] | None:
        registry = registry_factory()
        try:
            return _public_active(registry.get_active())
        finally:
            _close_registry(registry)

    def open_catalog(store: object) -> ElementCatalog:
        try:
            catalog_config = status_config_provider()
        except Exception:
            catalog_config = None
        return ElementCatalog(
            store,
            legacy_elements_provider=legacy_elements_provider,
            site=getattr(catalog_config, "site", "tiktok"),
            environment=getattr(
                catalog_config,
                "environment",
                "production",
            ),
        )

    def reserve_operation(
        store: object,
        *,
        actor_user_id: int,
        operation: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        pending_response: Mapping[str, object],
        pending_status_code: int = 202,
    ) -> tuple[dict[str, object], str]:
        digest = _operation_payload_hash(payload)
        reservation = store.reserve_management_operation(
            actor_user_id=actor_user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            payload_hash=digest,
            request_payload=payload,
            pending_response=pending_response,
            pending_status_code=pending_status_code,
        )
        return reservation, digest

    def finish_operation_failure(
        *,
        actor_user_id: int | None,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
        response: Mapping[str, object],
        status_code: int,
    ) -> None:
        if not actor_user_id or not payload_hash:
            return
        try:
            with _open_store(store_factory) as store:
                store.complete_management_operation(
                    actor_user_id=actor_user_id,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    response=response,
                    status_code=status_code,
                    failed=True,
                )
        except Exception:
            pass

    @blueprint.get("/api/selector-probe/elements")
    @allow_roles("administrator", "operator")
    def elements_route():
        try:
            query = _element_query()
            with _open_store(store_factory) as store:
                result = open_catalog(store).list(query)
        except (TypeError, ValueError) as error:
            code = (
                str(error)
                if str(error) in {"invalid_pagination", "invalid_filter"}
                else "invalid_filter"
            )
            return jsonify({"code": code}), 400
        except Exception:
            return jsonify({"code": "element_service_unavailable"}), 503
        return jsonify(
            {
                "items": [
                    public_element_summary(item) for item in result.items
                ],
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
                "revision": result.revision,
            }
        )

    @blueprint.post("/api/selector-probe/elements")
    @allow_roles("administrator")
    def create_element_route():
        try:
            actor_user_id, actor_username = _actor_identity()
            with _open_store(store_factory) as store:
                catalog = open_catalog(store)
                record = catalog.create_draft(
                    request.get_json(silent=True),
                    actor_user_id,
                    actor_username,
                )
                payload = _element_detail_payload(catalog, record)
        except Exception as error:
            return _element_error(error)
        return jsonify(payload), 201

    @blueprint.get("/api/selector-probe/elements/<element_id>")
    @allow_roles("administrator", "operator")
    def element_detail_route(element_id: str):
        try:
            with _open_store(store_factory) as store:
                catalog = open_catalog(store)
                record = catalog.get(element_id)
                if record is None:
                    raise ElementNotFoundError(element_id)
                try:
                    active = active_bundle()
                except Exception:
                    active = None
                active_definition = (
                    active.get("elements", {}).get(record.id)
                    if isinstance(active, Mapping)
                    and isinstance(active.get("elements"), Mapping)
                    else None
                )
                payload = _element_detail_payload(
                    catalog,
                    record,
                    active_definition=active_definition,
                )
        except Exception as error:
            return _element_error(error)
        return jsonify(payload)

    @blueprint.patch(
        "/api/selector-probe/elements/<element_id>/draft"
    )
    @allow_roles("administrator")
    def update_element_draft_route(element_id: str):
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"expected_revision", "contract"}
        ):
            return jsonify({"code": "invalid_element_request"}), 400
        try:
            actor_user_id, actor_username = _actor_identity()
            with _open_store(store_factory) as store:
                catalog = open_catalog(store)
                record = catalog.update_draft(
                    element_id,
                    {"contract": payload["contract"]},
                    payload["expected_revision"],
                    actor_user_id,
                    actor_username,
                )
                result = _element_detail_payload(catalog, record)
        except Exception as error:
            return _element_error(error)
        return jsonify(result)

    @blueprint.delete("/api/selector-probe/elements/<element_id>")
    @allow_roles("administrator")
    def delete_element_route(element_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping) or set(payload) != {
            "expected_revision"
        }:
            return jsonify({"code": "invalid_element_request"}), 400
        try:
            actor_user_id, actor_username = _actor_identity()
            with _open_store(store_factory) as store:
                open_catalog(store).delete(
                    element_id,
                    payload["expected_revision"],
                    actor_user_id,
                    actor_username,
                )
        except Exception as error:
            return _element_error(error)
        return "", 204

    def dispatch_element(element_id: str, *, request_type: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping) or set(payload) != {
            "expected_revision"
        }:
            return jsonify({"code": "invalid_element_request"}), 400
        try:
            actor_user_id, actor_username = _actor_identity()
            with _open_store(store_factory) as store:
                catalog = open_catalog(store)
                record = catalog.require_revision(
                    element_id,
                    payload["expected_revision"],
                )
                request_id = uuid.uuid4().hex
                accepted = store.reserve_element_request(
                    element_id=record.id,
                    request_type=request_type,
                    request_id=request_id,
                    expected_revision=record.revision,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
        except Exception as error:
            return _element_error(error)
        try:
            request_waker(str(accepted["request_id"]))
        except Exception:
            pass
        return jsonify(
            {
                "status": "accepted",
                "request_id": accepted["request_id"],
                "element_id": accepted["element_id"],
                "request_type": accepted["request_type"],
                "expected_revision": accepted["expected_revision"],
            }
        ), 202

    @blueprint.post(
        "/api/selector-probe/elements/<element_id>/probe"
    )
    @allow_roles("administrator", "operator")
    def probe_element_route(element_id: str):
        return dispatch_element(element_id, request_type="probe")

    @blueprint.post(
        "/api/selector-probe/elements/<element_id>/validate"
    )
    @allow_roles("administrator")
    def validate_element_route(element_id: str):
        return dispatch_element(element_id, request_type="validate")

    @blueprint.post(
        "/api/selector-probe/elements/<element_id>/migrate"
    )
    @allow_roles("administrator")
    def migrate_element_route(element_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping) or set(payload) != {
            "expected_revision"
        }:
            return jsonify({"code": "invalid_element_request"}), 400
        try:
            actor_user_id, actor_username = _actor_identity()
            with _open_store(store_factory) as store:
                catalog = open_catalog(store)
                record = catalog.create_legacy_migration(
                    element_id,
                    actor_user_id,
                    actor_username,
                    expected_revision=payload["expected_revision"],
                )
                result = _element_detail_payload(catalog, record)
        except Exception as error:
            return _element_error(error)
        return jsonify(result)

    @blueprint.get(
        "/api/selector-probe/element-requests/<request_id>"
    )
    @allow_roles("administrator", "operator")
    def element_request_route(request_id: str):
        try:
            with _open_store(store_factory) as store:
                element_request = store.get_element_request(request_id)
            if element_request is None:
                return jsonify({"code": "element_request_not_found"}), 404
            return jsonify(public_element_request(element_request))
        except ValueError:
            return jsonify({"code": "invalid_element_request"}), 400
        except Exception:
            return jsonify({"code": "element_service_unavailable"}), 503

    @blueprint.get("/api/selector-probe/status")
    def status_route():
        overview: object = None
        try:
            config = status_config_provider()
        except Exception:
            config = None
        try:
            with _open_store(store_factory) as store:
                runs = _history(store, "runs", limit=1, offset=0)
                overview_loader = getattr(
                    store,
                    "selector_probe_overview",
                    None,
                )
                if callable(overview_loader) and config is not None:
                    try:
                        overview = overview_loader(
                            site=config.site,
                            environment=config.environment,
                        )
                    except Exception:
                        overview = None
            latest = _public_run(runs[0]) if runs else None
        except Exception:
            latest = None
        try:
            active = active_bundle()
        except Exception:
            active = None
        registry_status: dict[str, object] = {"available": active is not None}
        if active is not None:
            registry_status["active_version"] = active["version"]
            if "bundle_hash" in active:
                registry_status["bundle_hash"] = active["bundle_hash"]
        payload = _safe_status_overview(
            overview,
            active=active,
            latest_run=latest,
            config=config,
            now=utcnow_fn(),
        )
        payload["registry"] = registry_status
        payload["latest_run"] = latest
        return jsonify(payload)

    @blueprint.get("/api/selector-probe/active")
    def active_route():
        try:
            active = active_bundle()
        except Exception:
            active = None
        if active is None:
            return jsonify({"error": "registry_unavailable"}), 503
        return jsonify(active)

    def management_list_response(kind: str, projector):
        legacy = "page" not in request.args and (
            getattr(g, "management_user", None) is None
            or "limit" in request.args
            or "offset" in request.args
        )
        if legacy:
            try:
                limit, offset = _pagination()
                with _open_store(store_factory) as store:
                    rows = _history(
                        store, kind, limit=limit, offset=offset
                    )
            except ValueError:
                return jsonify({"error": "invalid_pagination"}), 400
            except Exception:
                return jsonify({"error": "history_unavailable"}), 503
            legacy_projector = (
                _public_run if kind == "runs" else _public_version
            )
            items = [
                item
                for row in rows
                if (item := legacy_projector(row))
            ]
            return jsonify(
                {
                    "items": items,
                    "pagination": {
                        "limit": limit,
                        "offset": offset,
                        "count": len(items),
                    },
                }
            )
        try:
            page, page_size = _management_pagination()
            filters = {
                key: value
                for key in (
                    "status",
                    "failure_class",
                    "event_type",
                    "target_id",
                    "source",
                    "search",
                )
                if (value := request.args.get(key)) is not None
            }
            with _open_store(store_factory) as store:
                loader = getattr(store, "list_management_rows", None)
                if callable(loader):
                    rows, total, revision = loader(
                        kind,
                        page=page,
                        page_size=page_size,
                        filters=filters,
                    )
                elif kind in {"runs", "versions"}:
                    if (
                        "page" not in request.args
                        and "page_size" not in request.args
                        and not filters
                    ):
                        legacy_rows = _history(
                            store,
                            kind,
                            limit=50,
                            offset=0,
                        )
                        rows = legacy_rows[:page_size]
                        total = len(legacy_rows)
                    else:
                        rows = _history(
                            store,
                            kind,
                            limit=page_size,
                            offset=(page - 1) * page_size,
                        )
                        total = len(rows)
                    revision = 0
                else:
                    raise RuntimeError("management list unavailable")
            return jsonify(
                {
                    "items": [
                        item for row in rows if (item := projector(row))
                    ],
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "revision": revision,
                }
            )
        except ValueError:
            return jsonify({"code": "invalid_pagination"}), 400
        except Exception:
            return jsonify({"code": f"{kind}_unavailable"}), 503

    @blueprint.get("/api/selector-probe/runs")
    @allow_roles("administrator", "operator")
    def runs_route():
        return management_list_response("runs", _management_project_run)

    @blueprint.get("/api/selector-probe/runs/<run_id>")
    @allow_roles("administrator", "operator")
    def run_detail_route(run_id: str):
        try:
            with _open_store(store_factory) as store:
                value = store.management_run_detail(run_id)
        except Exception:
            return jsonify({"code": "runs_unavailable"}), 503
        if value is None:
            return jsonify({"code": "run_not_found"}), 404
        return jsonify(_management_project_run(value))

    @blueprint.get("/api/selector-probe/versions")
    @allow_roles("administrator", "operator")
    def versions_route():
        return management_list_response(
            "versions", _management_project_version
        )

    @blueprint.get("/api/selector-probe/versions/<version_id>")
    @allow_roles("administrator", "operator")
    def version_detail_route(version_id: str):
        try:
            with _open_store(store_factory) as store:
                value = store.management_version_detail(version_id)
        except Exception:
            return jsonify({"code": "versions_unavailable"}), 503
        if value is None:
            return jsonify({"code": "version_not_found"}), 404
        return jsonify(_management_project_version(value))

    @blueprint.get("/api/selector-probe/versions/<version_id>/diff")
    @allow_roles("administrator", "operator")
    def version_diff_route(version_id: str):
        try:
            with _open_store(store_factory) as store:
                value = store.management_version_diff(version_id)
        except Exception:
            return jsonify({"code": "versions_unavailable"}), 503
        if value is None:
            return jsonify({"code": "version_not_found"}), 404
        return jsonify(value)

    @blueprint.post(
        "/api/selector-probe/versions/<version_id>/rollback-validation"
    )
    @allow_roles("administrator")
    def rollback_validation_route(version_id: str):
        actor_id = None
        operation = f"rollback-validation:{version_id}"
        key = ""
        digest = ""
        try:
            payload = _strict_object(
                request.get_json(silent=True),
                required={"reason", "idempotency_key"},
            )
            reason = _request_text(payload["reason"], "reason")
            key = _request_text(
                payload["idempotency_key"],
                "idempotency_key",
                maximum=128,
            )
            actor_id, actor_name = _management_actor()
            with _open_store(store_factory) as store:
                reservation, digest = reserve_operation(
                    store,
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload={
                        "version_id": version_id,
                        "reason": reason,
                    },
                    pending_response={"code": "operation_in_progress"},
                    pending_status_code=409,
                )
                if not reservation["reserved"]:
                    return (
                        jsonify(reservation["response"]),
                        int(reservation["status_code"]),
                    )
                result = store.request_rollback_validation(
                    version_id,
                    actor_user_id=actor_id,
                    actor_username=actor_name,
                    reason=reason,
                )
                store.complete_management_operation(
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload_hash=digest,
                    response=result,
                    status_code=202,
                )
            return jsonify(result), 202
        except ManagementIdempotencyConflictError:
            return jsonify({"code": "idempotency_conflict"}), 409
        except KeyError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "version_not_found"},
                status_code=404,
            )
            return jsonify({"code": "version_not_found"}), 404
        except ValueError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "invalid_rollback_request"},
                status_code=400,
            )
            return jsonify({"code": "invalid_rollback_request"}), 400
        except Exception:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "rollback_unavailable"},
                status_code=503,
            )
            return jsonify({"code": "rollback_unavailable"}), 503

    @blueprint.get("/api/selector-probe/gates")
    @allow_roles("administrator", "operator")
    def gates_route():
        if (
            getattr(g, "management_user", None) is None
            and "page" not in request.args
        ):
            try:
                with _open_gate_service(gate_service_factory) as service:
                    items = [
                        _public_gate_decision(
                            service.check(strategy_id),
                            strategy_id=strategy_id,
                        )
                        for strategy_id in _gate_strategy_ids(service)
                    ]
            except Exception:
                return jsonify(
                    {"error": "gate_registry_unavailable"}
                ), 503
            return jsonify({"items": items, "count": len(items)})
        return management_list_response(
            "gates", _management_project_gate
        )

    def set_manual_gate(strategy_id: str, *, paused: bool):
        legacy = getattr(g, "management_user", None) is None
        actor_id = None
        operation = (
            f"manual-gate:{strategy_id}:"
            f"{'pause' if paused else 'resume'}"
        )
        key = ""
        digest = ""
        try:
            raw = request.get_json(silent=True)
            if legacy and paused and raw == {"reason": "operator_pause"}:
                with _open_gate_service(gate_service_factory) as service:
                    decision = service.set_manual_pause(
                        strategy_id, True, "operator"
                    )
                    return jsonify(
                        _public_gate_decision(
                            decision, strategy_id=strategy_id
                        )
                    )
            if legacy and not paused and raw is None:
                with _open_gate_service(gate_service_factory) as service:
                    decision = service.set_manual_pause(
                        strategy_id, False, "operator"
                    )
                    return jsonify(
                        _public_gate_decision(
                            decision, strategy_id=strategy_id
                        )
                    )
            payload = _strict_object(
                raw,
                required={
                    "reason",
                    "expected_revision",
                    "idempotency_key",
                },
            )
            reason = _request_text(payload["reason"], "reason")
            revision = _request_revision(payload["expected_revision"])
            key = _request_text(
                payload["idempotency_key"],
                "idempotency_key",
                maximum=128,
            )
            actor_id, actor_name = _management_actor()
            with _open_gate_service(gate_service_factory) as service:
                store = getattr(service, "store", service)
                reservation, digest = reserve_operation(
                    store,
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload={
                        "strategy_id": strategy_id,
                        "paused": paused,
                        "reason": reason,
                        "expected_revision": revision,
                    },
                    pending_response={"code": "operation_in_progress"},
                    pending_status_code=409,
                )
                if not reservation["reserved"]:
                    return (
                        jsonify(reservation["response"]),
                        int(reservation["status_code"]),
                    )
                value = store.set_manual_gate_cas(
                    strategy_id,
                    paused=paused,
                    expected_revision=revision,
                    actor_user_id=actor_id,
                    actor_username=actor_name,
                    reason=reason,
                )
                service.project_strategy_ids((strategy_id,))
                value.update(
                    {
                        "effective_status": (
                            "paused" if value["reasons"] else "active"
                        ),
                        "strategy_name": "",
                        "affected_action_ids": [],
                    }
                )
                public = _management_project_gate(value)
                store.complete_management_operation(
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload_hash=digest,
                    response=public,
                    status_code=200,
                )
            return jsonify(public)
        except ManagementIdempotencyConflictError:
            return jsonify({"code": "idempotency_conflict"}), 409
        except StaleManagementRevisionError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "stale_revision"},
                status_code=409,
            )
            return jsonify({"code": "stale_revision"}), 409
        except ValueError:
            if legacy and paused:
                return jsonify({"error": "invalid_pause_request"}), 400
            response = {"code": "invalid_gate_request"}
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response=response,
                status_code=400,
            )
            return jsonify(response), 400
        except Exception:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "gate_registry_unavailable"},
                status_code=503,
            )
            return jsonify({"code": "gate_registry_unavailable"}), 503

    @blueprint.post(
        "/api/selector-probe/strategies/<strategy_id>/pause"
    )
    @allow_roles("administrator")
    def pause_strategy_route(strategy_id: str):
        return set_manual_gate(strategy_id, paused=True)

    @blueprint.post(
        "/api/selector-probe/strategies/<strategy_id>/resume"
    )
    @allow_roles("administrator")
    def resume_strategy_route(strategy_id: str):
        return set_manual_gate(strategy_id, paused=False)

    @blueprint.get("/api/selector-probe/alerts")
    @allow_roles("administrator", "operator")
    def alerts_route():
        return management_list_response(
            "alerts", _management_project_alert
        )

    @blueprint.get("/api/selector-probe/alerts/<int:alert_id>")
    @allow_roles("administrator", "operator")
    def alert_detail_route(alert_id: int):
        try:
            with _open_store(store_factory) as store:
                row = store.connection.execute(
                    "SELECT * FROM probe_alerts WHERE id = ?",
                    (alert_id,),
                ).fetchone()
                if row is not None:
                    value = dict(row)
                    strategy_ids = _strings(value["strategy_ids_json"])
                    if strategy_ids:
                        placeholders = ",".join(
                            "?" for _ in strategy_ids
                        )
                        value["gate_active"] = (
                            store.connection.execute(
                                f"""
                                SELECT 1 FROM strategy_gate_reasons
                                WHERE strategy_id IN ({placeholders})
                                  AND cleared_at IS NULL
                                LIMIT 1
                                """,
                                strategy_ids,
                            ).fetchone()
                            is not None
                        )
                    else:
                        value["gate_active"] = False
        except Exception:
            return jsonify({"code": "alerts_unavailable"}), 503
        if row is None:
            return jsonify({"code": "alert_not_found"}), 404
        return jsonify(_management_project_alert(value))

    def transition_alert_route(alert_id: int, *, status: str):
        actor_id = None
        operation = f"alert:{alert_id}:{status}"
        key = ""
        digest = ""
        try:
            required = {"idempotency_key"}
            if status == "resolved":
                required |= {"reason", "expected_revision"}
            payload = _strict_object(
                request.get_json(silent=True),
                required=required,
            )
            key = _request_text(
                payload["idempotency_key"],
                "idempotency_key",
                maximum=128,
            )
            reason = (
                _request_text(payload["reason"], "reason")
                if status == "resolved"
                else ""
            )
            revision = (
                _request_revision(payload["expected_revision"])
                if status == "resolved"
                else None
            )
            actor_id, actor_name = _management_actor()
            with _open_store(store_factory) as store:
                reservation, digest = reserve_operation(
                    store,
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload={
                        "alert_id": alert_id,
                        "status": status,
                        "reason": reason,
                        "expected_revision": revision,
                    },
                    pending_response={"code": "operation_in_progress"},
                    pending_status_code=409,
                )
                if not reservation["reserved"]:
                    return (
                        jsonify(reservation["response"]),
                        int(reservation["status_code"]),
                    )
                result = _management_project_alert(
                    store.transition_alert_cas(
                        alert_id,
                        status=status,
                        expected_revision=revision,
                        actor_user_id=actor_id,
                        actor_username=actor_name,
                        reason=reason,
                    )
                )
                store.complete_management_operation(
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload_hash=digest,
                    response=result,
                    status_code=200,
                )
            return jsonify(result)
        except ManagementIdempotencyConflictError:
            return jsonify({"code": "idempotency_conflict"}), 409
        except KeyError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "alert_not_found"},
                status_code=404,
            )
            return jsonify({"code": "alert_not_found"}), 404
        except StaleManagementRevisionError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "stale_revision"},
                status_code=409,
            )
            return jsonify({"code": "stale_revision"}), 409
        except GateStillActiveError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "gate_still_active"},
                status_code=409,
            )
            return jsonify({"code": "gate_still_active"}), 409
        except ValueError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "invalid_alert_request"},
                status_code=400,
            )
            return jsonify({"code": "invalid_alert_request"}), 400
        except Exception:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "alerts_unavailable"},
                status_code=503,
            )
            return jsonify({"code": "alerts_unavailable"}), 503

    @blueprint.post(
        "/api/selector-probe/alerts/<int:alert_id>/acknowledge"
    )
    @allow_roles("administrator", "operator")
    def acknowledge_alert_route(alert_id: int):
        return transition_alert_route(alert_id, status="acknowledged")

    @blueprint.post(
        "/api/selector-probe/alerts/<int:alert_id>/resolve"
    )
    @allow_roles("administrator")
    def resolve_alert_route(alert_id: int):
        return transition_alert_route(alert_id, status="resolved")

    @blueprint.get(
        "/api/selector-probe/alerts/<int:alert_id>/screenshot"
    )
    @allow_roles("administrator", "operator")
    def alert_screenshot_route(alert_id: int):
        try:
            with _open_store(store_factory) as store:
                row = store.connection.execute(
                    """
                    SELECT screenshot_path FROM probe_alerts
                    WHERE id = ?
                    """,
                    (alert_id,),
                ).fetchone()
            if row is None:
                return jsonify({"code": "alert_not_found"}), 404
            if not row["screenshot_path"]:
                return jsonify({"code": "screenshot_not_found"}), 404
            path = resolve_evidence_path(
                evidence_root,
                str(row["screenshot_path"]),
                must_exist=True,
            )
            response = send_file(path, conditional=False)
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            return response
        except (OSError, ValueError):
            return jsonify({"code": "screenshot_not_found"}), 404
        except Exception:
            return jsonify({"code": "screenshot_unavailable"}), 503

    @blueprint.get("/api/selector-probe/audit")
    @allow_roles("administrator")
    def audit_route():
        def project(row):
            if not isinstance(row, Mapping):
                return {}
            return {
                "id": row.get("id"),
                "actor_username": row.get("actor_username"),
                "event_type": row.get("event_type"),
                "target_type": row.get("target_type"),
                "target_id": row.get("target_id"),
                "result": row.get("result"),
                "details": _safe_audit_details(row.get("details_json")),
                "created_at": row.get("created_at"),
            }

        return management_list_response("audit", project)

    def prepare_settings(
        raw_settings: Mapping[str, object],
        submitted: object,
        *,
        profile_changes: object = None,
        submitted_secrets: object = None,
    ) -> dict[str, object]:
        import copy

        payload = submitted if isinstance(submitted, Mapping) else {}
        updated = copy.deepcopy(dict(raw_settings))
        probe = updated.setdefault("selector_probe", {})
        if not isinstance(probe, dict):
            probe = {}
            updated["selector_probe"] = probe
        scalar_map = {
            "enabled": "enabled",
            "schedule_time": "daily_time",
            "timezone": "timezone",
            "target_origin": "target_url",
            "freshness_hours": "freshness_hours",
            "retry_policy": "retry_policy",
        }
        for public_key, private_key in scalar_map.items():
            if public_key in payload:
                probe[private_key] = payload[public_key]
        if "rollout_mode" in payload:
            mode = str(payload["rollout_mode"])
            if mode not in {"observe", "publish", "enforce"}:
                raise ValueError("invalid rollout mode")
            probe["rollout_mode"] = mode
            probe["observe_only"] = mode == "observe"
        current_ids = [
            item
            for item in probe.get("test_profile_ids", [])
            if isinstance(item, str) and item
        ]
        dedicated_ids = {
            item
            for item in probe.get("dedicated_test_profile_ids", [])
            if isinstance(item, str) and item
        }
        by_ref = {_profile_ref(item): item for item in current_ids}
        profiles = payload.get("profiles")
        if profiles is not None:
            if not isinstance(profiles, Sequence) or isinstance(
                profiles, (str, bytes, bytearray)
            ):
                raise ValueError("invalid profiles")
            selected: list[str] = []
            for item in profiles:
                if not isinstance(item, Mapping):
                    raise ValueError("invalid profile")
                ref = item.get("profile_ref")
                if not isinstance(ref, str) or ref not in by_ref:
                    raise ValueError("unknown profile_ref")
                if item.get("dedicated_test") is not True:
                    raise ValueError("profile is not dedicated")
                if by_ref[ref] not in dedicated_ids:
                    raise ValueError("profile is not dedicated")
                selected.append(by_ref[ref])
            current_ids = list(dict.fromkeys(selected))
        changes = (
            profile_changes
            if isinstance(profile_changes, Mapping)
            else {}
        )
        remove_refs = changes.get("remove", [])
        if remove_refs:
            if not isinstance(remove_refs, Sequence) or isinstance(
                remove_refs, (str, bytes, bytearray)
            ):
                raise ValueError("invalid profile removal")
            unknown = [
                item
                for item in remove_refs
                if not isinstance(item, str) or item not in by_ref
            ]
            if unknown:
                raise ValueError("unknown profile_ref")
            removed = {by_ref[item] for item in remove_refs}
            current_ids = [
                item for item in current_ids if item not in removed
            ]
            dedicated_ids -= removed
        additions = changes.get("add", [])
        if additions:
            if not isinstance(additions, Sequence) or isinstance(
                additions, (str, bytes, bytearray)
            ):
                raise ValueError("invalid profile addition")
            for item in additions:
                raw_id = _request_text(
                    item, "profile_id", maximum=256
                )
                if raw_id not in current_ids:
                    current_ids.append(raw_id)
                dedicated_ids.add(raw_id)
        probe["test_profile_ids"] = current_ids
        probe["dedicated_test_profile_ids"] = [
            item for item in current_ids if item in dedicated_ids
        ]
        model = payload.get("model")
        if isinstance(model, Mapping) and "id" in model:
            probe["model_id"] = _request_text(
                model["id"], "model_id", maximum=128
            )
        redis_public = payload.get("redis")
        if isinstance(redis_public, Mapping):
            redis_private = probe.setdefault("redis", {})
            if not isinstance(redis_private, dict):
                redis_private = {}
                probe["redis"] = redis_private
            if "namespace" in redis_public:
                redis_private["namespace"] = _request_text(
                    redis_public["namespace"],
                    "redis_namespace",
                    maximum=128,
                )
        webhook_public = payload.get("webhook")
        if isinstance(webhook_public, Mapping):
            webhook_private = probe.setdefault("webhook", {})
            if not isinstance(webhook_private, dict):
                webhook_private = {}
                probe["webhook"] = webhook_private
            for name in (
                "enabled",
                "type",
                "timeout_seconds",
                "retry_policy",
            ):
                if name in webhook_public:
                    webhook_private[name] = webhook_public[name]
        secret_values = (
            submitted_secrets
            if isinstance(submitted_secrets, Mapping)
            else {}
        )
        allowed_secrets = {
            "model_api_key",
            "redis_password",
            "webhook_signing_secret",
            "webhook_url",
        }
        if not set(secret_values).issubset(allowed_secrets):
            raise ValueError("invalid secret")
        selected_model_id = str(probe.get("model_id") or "")
        for name, value in secret_values.items():
            if not isinstance(value, str):
                raise ValueError("invalid secret")
            if not value:
                continue
            if name == "model_api_key":
                models = updated.setdefault("models", {})
                items = models.get("items", []) if isinstance(models, dict) else []
                target = next(
                    (
                        item
                        for item in items
                        if isinstance(item, dict)
                        and item.get("id") == selected_model_id
                    ),
                    None,
                )
                if target is None:
                    raise ValueError("unknown model")
                target["api_key"] = value
            elif name == "redis_password":
                probe.setdefault("redis", {})["password"] = value
            elif name == "webhook_signing_secret":
                probe.setdefault("webhook", {})[
                    "signing_secret"
                ] = value
            elif name == "webhook_url":
                probe.setdefault("webhook", {})["url"] = value
        return updated

    def reconcile_settings_publications(
        store,
        raw_settings: Mapping[str, object],
    ) -> None:
        loader = getattr(store, "pending_settings_publications", None)
        completer = getattr(store, "complete_settings_publication", None)
        failer = getattr(store, "fail_settings_publication", None)
        if not (
            callable(loader)
            and callable(completer)
            and callable(failer)
        ):
            return
        private_reference = _settings_private_reference(raw_settings)
        for intent in loader():
            intent_id = str(intent.get("id") or "")
            staged_revision = intent.get("staged_revision")
            candidate = intent.get("candidate")
            if (
                not intent_id
                or isinstance(staged_revision, bool)
                or not isinstance(staged_revision, int)
                or not isinstance(candidate, Mapping)
            ):
                continue
            if intent.get("private_reference") == private_reference:
                response = dict(candidate)
                response["revision"] = staged_revision
                completer(
                    intent_id,
                    private_reference=private_reference,
                    response=response,
                    reconciled=True,
                    now=utcnow_fn(),
                )
            else:
                failer(
                    intent_id,
                    error_code="settings_write_not_observed",
                    response={
                        "code": "settings_write_not_observed",
                        "revision": staged_revision,
                    },
                    status_code=503,
                    now=utcnow_fn(),
                )

    def settings_revision() -> int:
        raw_settings = settings_provider()
        with _open_store(store_factory) as store:
            reconcile_settings_publications(store, raw_settings)
            return store.current_revision("settings")

    def run_preflight(
        raw_settings: Mapping[str, object],
        candidate: Mapping[str, object],
    ) -> dict[str, str]:
        checks = _settings_checks(candidate)
        if settings_preflight_runner is None:
            return {
                name: "failed"
                for name in checks
            }
        try:
            parameter_count = len(
                inspect.signature(settings_preflight_runner).parameters
            )
        except (TypeError, ValueError):
            parameter_count = 2
        custom = (
            settings_preflight_runner(raw_settings, candidate)
            if parameter_count >= 2
            else settings_preflight_runner(candidate)
        )
        if isinstance(custom, Mapping):
            for name in checks:
                if custom.get(name) in {"passed", "failed"}:
                    checks[name] = str(custom[name])
                else:
                    checks[name] = "failed"
        else:
            checks = {name: "failed" for name in checks}
        profiles = candidate.get("profiles", [])
        if (
            not isinstance(profiles, Sequence)
            or isinstance(profiles, (str, bytes, bytearray))
            or len(profiles) < 2
            or len(
                {
                    item.get("profile_ref")
                    for item in profiles
                    if isinstance(item, Mapping)
                }
            )
            != len(profiles)
            or not all(
                isinstance(item, Mapping)
                and item.get("dedicated_test") is True
                for item in profiles
            )
        ):
            checks["profiles"] = "failed"
        webhook_value = candidate.get("webhook", {})
        if (
            not isinstance(webhook_value, Mapping)
            or webhook_value.get("enabled") is not True
        ):
            checks["webhook"] = "failed"
        if isinstance(candidate, dict):
            if checks["profiles"] == "passed":
                for item in candidate.get("profiles", []):
                    if isinstance(item, dict):
                        item["status"] = "healthy"
            model = candidate.get("model")
            if isinstance(model, dict):
                model["status"] = (
                    "passed"
                    if checks["model"] == "passed"
                    else "failed"
                )
            redis_value = candidate.get("redis")
            if isinstance(redis_value, dict):
                redis_value["aof_enabled"] = (
                    checks["redis_aof"] == "passed"
                )
                redis_value["eviction_policy"] = (
                    "noeviction"
                    if checks["redis_eviction"] == "passed"
                    else ""
                )
                redis_value["status"] = (
                    "healthy"
                    if checks["redis_aof"] == "passed"
                    and checks["redis_eviction"] == "passed"
                    else "failed"
                )
            webhook = candidate.get("webhook")
            if isinstance(webhook, dict):
                webhook["status"] = (
                    "passed"
                    if checks["webhook"] == "passed"
                    else "failed"
                )
        return checks

    def registry_lease_active() -> bool:
        registry = registry_factory()
        try:
            redis_client = getattr(registry, "redis", None)
            keys = getattr(registry, "keys", None)
            lease_key = getattr(keys, "lease", None)
            if redis_client is None or not isinstance(lease_key, str):
                return False
            return bool(redis_client.get(lease_key))
        finally:
            _close_registry(registry)

    def apply_preflight_health(
        projected: dict[str, object],
        health: object,
    ) -> dict[str, object]:
        if not isinstance(health, Mapping):
            return projected
        profiles = health.get("profiles", [])
        by_ref = {
            str(item.get("profile_ref")): item
            for item in profiles
            if isinstance(item, Mapping) and item.get("profile_ref")
        }
        for item in projected.get("profiles", []):
            if not isinstance(item, dict):
                continue
            trusted = by_ref.get(str(item.get("profile_ref")))
            if (
                isinstance(trusted, Mapping)
                and trusted.get("dedicated_test")
                == item.get("dedicated_test")
            ):
                item["status"] = str(
                    trusted.get("status") or "unknown"
                )
        checks = health.get("checks", {})
        if isinstance(checks, Mapping):
            model = projected.get("model")
            redis_value = projected.get("redis")
            webhook = projected.get("webhook")
            if isinstance(model, dict):
                model["status"] = (
                    "passed"
                    if checks.get("model") == "passed"
                    else "failed"
                )
            if isinstance(redis_value, dict):
                redis_value["aof_enabled"] = (
                    checks.get("redis_aof") == "passed"
                )
                redis_value["eviction_policy"] = (
                    "noeviction"
                    if checks.get("redis_eviction") == "passed"
                    else ""
                )
                redis_value["status"] = (
                    "healthy"
                    if checks.get("redis_aof") == "passed"
                    and checks.get("redis_eviction") == "passed"
                    else "failed"
                )
            if isinstance(webhook, dict):
                webhook["status"] = (
                    "passed"
                    if checks.get("webhook") == "passed"
                    else "failed"
                )
        return projected

    def projected_settings_with_health() -> dict[str, object]:
        raw = settings_provider()
        with _open_store(store_factory) as store:
            reconcile_settings_publications(store, raw)
            revision = store.current_revision("settings")
            projected = _selector_settings_projection(
                raw, revision=revision
            )
            workspace = (
                f"{projected['site']}:{projected['environment']}"
            )
            health = store.management_preflight_health(workspace)
        if isinstance(health, Mapping):
            try:
                checked_at = datetime.fromisoformat(
                    str(health.get("checked_at") or "").replace(
                        "Z", "+00:00"
                    )
                )
            except ValueError:
                health = None
            else:
                if (
                    health.get("base_revision") != revision
                    or health.get("canonical_fingerprint")
                    != _settings_candidate_fingerprint(projected)
                    or utcnow_fn().astimezone(UTC) - checked_at
                    > timedelta(
                        seconds=PREFLIGHT_TOKEN_MAX_AGE_SECONDS
                    )
                ):
                    health = None
        return apply_preflight_health(projected, health)

    @blueprint.get("/api/selector-probe/settings")
    @allow_roles("administrator", "operator")
    def settings_route():
        try:
            return jsonify(projected_settings_with_health())
        except Exception:
            return jsonify({"code": "settings_unavailable"}), 503

    @blueprint.get("/api/selector-probe/settings/profiles")
    @allow_roles("administrator", "operator")
    def settings_profiles_route():
        try:
            projected = projected_settings_with_health()
            return jsonify(
                {
                    "items": projected["profiles"],
                    "revision": projected["revision"],
                }
            )
        except Exception:
            return jsonify({"code": "settings_unavailable"}), 503

    @blueprint.post("/api/selector-probe/settings/preflight")
    @allow_roles("administrator")
    def settings_preflight_route():
        try:
            payload = _strict_object(
                request.get_json(silent=True),
                required={"expected_revision", "settings"},
                optional={"candidate_fingerprint"},
            )
            expected = _request_revision(payload["expected_revision"])
            client_fingerprint = (
                _request_text(
                    payload["candidate_fingerprint"],
                    "candidate_fingerprint",
                    maximum=128,
                )
                if "candidate_fingerprint" in payload
                else ""
            )
            current_revision = settings_revision()
            if expected != current_revision:
                return jsonify({"code": "stale_revision"}), 409
            actor_id, actor_name = _management_actor()
            prepared = prepare_settings(
                settings_provider(), payload["settings"]
            )
            candidate = _selector_settings_projection(
                prepared, revision=expected
            )
            checks = run_preflight(prepared, candidate)
            checked_at = utcnow_fn().astimezone(UTC).isoformat()
            canonical_fingerprint = _settings_candidate_fingerprint(
                candidate
            )
            token_payload = {
                "actor": actor_id,
                "workspace": (
                    f"{candidate['site']}:{candidate['environment']}"
                ),
                "base_revision": expected,
                "client_fingerprint": client_fingerprint,
                "canonical_fingerprint": canonical_fingerprint,
                "checked_at": checked_at,
            }
            token = URLSafeTimedSerializer(
                _settings_secret(),
                salt="selector-probe-settings-preflight",
            ).dumps(token_payload)
            status = (
                "passed"
                if all(value == "passed" for value in checks.values())
                else "failed"
            )
            with _open_store(store_factory) as store:
                store.save_management_preflight_health(
                    f"{candidate['site']}:{candidate['environment']}",
                    {
                        "checks": checks,
                        "profiles": candidate.get("profiles", []),
                        "base_revision": expected,
                        "canonical_fingerprint": (
                            canonical_fingerprint
                        ),
                    },
                    checked_at=checked_at,
                )
            return jsonify(
                {
                    "status": status,
                    "base_revision": expected,
                    "candidate_fingerprint": client_fingerprint,
                    "preflight_token": token,
                    "checked_at": checked_at,
                    "checks": checks,
                    "settings": candidate,
                    "profiles": candidate.get("profiles", []),
                }
            )
        except ValueError:
            return jsonify({"code": "invalid_settings_request"}), 400
        except Exception:
            return jsonify({"code": "preflight_unavailable"}), 503

    @blueprint.patch("/api/selector-probe/settings")
    @allow_roles("administrator")
    def update_settings_route():
        actor_id = None
        operation = "settings:update"
        key = ""
        digest = ""
        try:
            payload = _strict_object(
                request.get_json(silent=True),
                required={
                    "expected_revision",
                    "reason",
                    "idempotency_key",
                    "settings",
                },
                optional={
                    "preflight_token",
                    "candidate_fingerprint",
                    "preflight_checked_at",
                    "profile_changes",
                    "secrets",
                },
            )
            expected = _request_revision(payload["expected_revision"])
            reason_value = payload["reason"]
            if not isinstance(reason_value, str) or len(reason_value) > 500:
                raise ValueError("invalid reason")
            key = _request_text(
                payload["idempotency_key"],
                "idempotency_key",
                maximum=128,
            )
            actor_id, actor_name = _management_actor()
            with settings_lock:
                with _open_store(store_factory) as store:
                    raw_before = settings_provider()
                    reconcile_settings_publications(store, raw_before)
                    operation_payload = {
                        key_name: value
                        for key_name, value in payload.items()
                        if key_name != "idempotency_key"
                    }
                    reservation, digest = reserve_operation(
                        store,
                        actor_user_id=actor_id,
                        operation=operation,
                        idempotency_key=key,
                        payload=operation_payload,
                        pending_response={
                            "code": "operation_in_progress"
                        },
                        pending_status_code=409,
                    )
                    if not reservation["reserved"]:
                        return (
                            jsonify(reservation["response"]),
                            int(reservation["status_code"]),
                        )
                    def settings_failure(code: str, status_code: int):
                        response = {"code": code}
                        store.complete_management_operation(
                            actor_user_id=actor_id,
                            operation=operation,
                            idempotency_key=key,
                            payload_hash=digest,
                            response=response,
                            status_code=status_code,
                            failed=True,
                        )
                        return jsonify(response), status_code
                    if store.current_revision("settings") != expected:
                        return settings_failure("stale_revision", 409)
                    before = _selector_settings_projection(
                        raw_before, revision=expected
                    )
                    prepared = prepare_settings(
                        raw_before,
                        payload["settings"],
                        profile_changes=payload.get("profile_changes"),
                        submitted_secrets=payload.get("secrets"),
                    )
                    candidate = _selector_settings_projection(
                        prepared, revision=expected
                    )
                    dangerous = [
                        name
                        for name in (
                            "enabled",
                            "rollout_mode",
                            "target_origin",
                            "profiles",
                            "model",
                            "redis",
                        )
                        if before.get(name) != candidate.get(name)
                    ]
                    if dangerous and not str(reason_value).strip():
                        return settings_failure("reason_required", 400)
                    if (
                        before["rollout_mode"]
                        != candidate["rollout_mode"]
                    ):
                        publishing = store.connection.execute(
                            """
                            SELECT 1
                            FROM selector_versions
                            WHERE status = 'publishing'
                            UNION ALL
                            SELECT 1
                            FROM publication_outbox
                            WHERE status IN ('pending', 'processing')
                            UNION ALL
                            SELECT 1
                            FROM element_request_outbox
                            WHERE status = 'publishing'
                            LIMIT 1
                            """
                        ).fetchone()
                        if publishing is not None:
                            return settings_failure(
                                "publication_in_progress", 409
                            )
                        if registry_lease_active():
                            return settings_failure(
                                "publication_lease_active", 409
                            )
                    if candidate["rollout_mode"] == "enforce":
                        token = payload.get("preflight_token")
                        client_fingerprint = payload.get(
                            "candidate_fingerprint"
                        )
                        client_checked_at = payload.get(
                            "preflight_checked_at"
                        )
                        if (
                            not isinstance(token, str)
                            or not token
                            or not isinstance(client_fingerprint, str)
                            or not client_fingerprint
                            or not isinstance(client_checked_at, str)
                            or not client_checked_at
                        ):
                            return settings_failure(
                                "preflight_required", 409
                            )
                        try:
                            token_data = URLSafeTimedSerializer(
                                _settings_secret(),
                                salt=(
                                    "selector-probe-settings-preflight"
                                ),
                            ).loads(
                                token,
                                max_age=PREFLIGHT_TOKEN_MAX_AGE_SECONDS,
                            )
                        except (BadSignature, SignatureExpired):
                            return settings_failure(
                                "invalid_preflight_token", 409
                            )
                        canonical_fingerprint = _settings_candidate_fingerprint(
                            candidate
                        )
                        expected_token = {
                            "actor": actor_id,
                            "workspace": (
                                f"{candidate['site']}:"
                                f"{candidate['environment']}"
                            ),
                            "base_revision": expected,
                            "client_fingerprint": client_fingerprint,
                            "canonical_fingerprint": (
                                canonical_fingerprint
                            ),
                            "checked_at": client_checked_at,
                        }
                        if not isinstance(token_data, Mapping) or any(
                            token_data.get(name) != value
                            for name, value in expected_token.items()
                        ):
                            return settings_failure(
                                "invalid_preflight_token", 409
                            )
                        if not all(
                            value == "passed"
                            for value in run_preflight(
                                prepared, candidate
                            ).values()
                        ):
                            return settings_failure(
                                "preflight_failed", 409
                            )
                    staged_candidate = _selector_settings_projection(
                        prepared, revision=expected + 1
                    )
                    private_reference = _settings_private_reference(
                        prepared
                    )
                    intent = store.stage_settings_publication(
                        actor_user_id=actor_id,
                        actor_username=actor_name,
                        operation=operation,
                        idempotency_key=key,
                        payload_hash=digest,
                        expected_revision=expected,
                        candidate=staged_candidate,
                        candidate_fingerprint=(
                            _settings_publication_fingerprint(
                                staged_candidate
                            )
                        ),
                        private_reference=private_reference,
                        reason=str(reason_value),
                        dangerous_changes=dangerous,
                        now=utcnow_fn(),
                    )
                    staged_revision = int(intent["staged_revision"])
                    try:
                        persisted = settings_mutator(
                            lambda _current: prepared
                        )
                    except Exception:
                        try:
                            store.fail_settings_publication(
                                str(intent["id"]),
                                error_code="settings_write_failed",
                                response={
                                    "code": "settings_write_failed",
                                    "revision": staged_revision,
                                },
                                status_code=503,
                                now=utcnow_fn(),
                            )
                        except Exception:
                            pass
                        return jsonify(
                            {
                                "code": "settings_write_failed",
                                "revision": staged_revision,
                            }
                        ), 503
                    persisted_settings = (
                        persisted
                        if isinstance(persisted, Mapping)
                        else settings_provider()
                    )
                    actual_reference = _settings_private_reference(
                        persisted_settings
                    )
                    result = _selector_settings_projection(
                        persisted_settings,
                        revision=staged_revision,
                    )
                    if actual_reference != private_reference:
                        try:
                            store.fail_settings_publication(
                                str(intent["id"]),
                                error_code="settings_write_mismatch",
                                response={
                                    "code": "settings_write_mismatch",
                                    "revision": staged_revision,
                                },
                                status_code=503,
                                now=utcnow_fn(),
                            )
                        except Exception:
                            pass
                        return jsonify(
                            {
                                "code": "settings_write_mismatch",
                                "revision": staged_revision,
                            }
                        ), 503
                    try:
                        store.complete_settings_publication(
                            str(intent["id"]),
                            private_reference=actual_reference,
                            response=result,
                            now=utcnow_fn(),
                        )
                    except Exception:
                        return jsonify(
                            {
                                "code": "settings_reconcile_pending",
                                "revision": staged_revision,
                            }
                        ), 202
                return jsonify(result)
        except ManagementIdempotencyConflictError:
            return jsonify({"code": "idempotency_conflict"}), 409
        except ValueError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "invalid_settings_request"},
                status_code=400,
            )
            return jsonify({"code": "invalid_settings_request"}), 400
        except Exception:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "settings_unavailable"},
                status_code=503,
            )
            return jsonify({"code": "settings_unavailable"}), 503

    @blueprint.post(
        "/api/selector-probe/settings/secrets/<secret_name>/clear"
    )
    @allow_roles("administrator")
    def clear_settings_secret_route(secret_name: str):
        actor_id: int | None = None
        operation = f"settings:clear:{secret_name}"
        key = ""
        digest = ""
        paths = {
            "model_api_key": "model",
            "redis_password": "redis",
            "webhook_signing_secret": "webhook",
        }
        if secret_name not in paths:
            return jsonify({"code": "secret_not_found"}), 404
        try:
            payload = _strict_object(
                request.get_json(silent=True),
                required={
                    "expected_revision",
                    "reason",
                    "idempotency_key",
                },
            )
            expected = _request_revision(payload["expected_revision"])
            reason = _request_text(payload["reason"], "reason")
            key = _request_text(
                payload["idempotency_key"],
                "idempotency_key",
                maximum=128,
            )
            actor_id, actor_name = _management_actor()
            with settings_lock:
                with _open_store(store_factory) as store:
                    raw_before = settings_provider()
                    reconcile_settings_publications(store, raw_before)
                    reservation, digest = reserve_operation(
                        store,
                        actor_user_id=actor_id,
                        operation=operation,
                        idempotency_key=key,
                        payload={
                            "secret_name": secret_name,
                            "expected_revision": expected,
                            "reason": reason,
                        },
                        pending_response={
                            "code": "operation_in_progress"
                        },
                        pending_status_code=409,
                    )
                    if not reservation["reserved"]:
                        return (
                            jsonify(reservation["response"]),
                            int(reservation["status_code"]),
                        )
                    if store.current_revision("settings") != expected:
                        response = {"code": "stale_revision"}
                        store.complete_management_operation(
                            actor_user_id=actor_id,
                            operation=operation,
                            idempotency_key=key,
                            payload_hash=digest,
                            response=response,
                            status_code=409,
                            failed=True,
                        )
                        return jsonify(response), 409

                    import copy

                    prepared = copy.deepcopy(dict(raw_before))

                    def clear_secret(current):
                        probe = current.setdefault("selector_probe", {})
                        if secret_name == "model_api_key":
                            model_id = str(probe.get("model_id") or "")
                            for item in current.get("models", {}).get(
                                "items", []
                            ):
                                if (
                                    isinstance(item, dict)
                                    and item.get("id") == model_id
                                ):
                                    item["api_key"] = ""
                        elif secret_name == "redis_password":
                            probe.setdefault("redis", {})["password"] = ""
                        else:
                            probe.setdefault("webhook", {})[
                                "signing_secret"
                            ] = ""
                        return current

                    prepared = clear_secret(prepared)
                    staged_candidate = _selector_settings_projection(
                        prepared, revision=expected + 1
                    )
                    private_reference = _settings_private_reference(
                        prepared
                    )
                    intent = store.stage_settings_publication(
                        actor_user_id=actor_id,
                        actor_username=actor_name,
                        operation=operation,
                        idempotency_key=key,
                        payload_hash=digest,
                        expected_revision=expected,
                        candidate=staged_candidate,
                        candidate_fingerprint=(
                            _settings_publication_fingerprint(
                                staged_candidate
                            )
                        ),
                        private_reference=private_reference,
                        reason=reason,
                        dangerous_changes=[f"secret:{secret_name}"],
                        now=utcnow_fn(),
                    )
                    revision = int(intent["staged_revision"])
                    try:
                        updated = settings_mutator(
                            lambda _current: prepared
                        )
                    except Exception:
                        try:
                            store.fail_settings_publication(
                                str(intent["id"]),
                                error_code="settings_write_failed",
                                response={
                                    "code": "settings_write_failed",
                                    "revision": revision,
                                },
                                status_code=503,
                                now=utcnow_fn(),
                            )
                        except Exception:
                            pass
                        return jsonify(
                            {
                                "code": "settings_write_failed",
                                "revision": revision,
                            }
                        ), 503
                    if not isinstance(updated, Mapping):
                        updated = settings_provider()
                    actual_reference = _settings_private_reference(updated)
                    result = _selector_settings_projection(
                        updated, revision=revision
                    )
                    if actual_reference != private_reference:
                        try:
                            store.fail_settings_publication(
                                str(intent["id"]),
                                error_code="settings_write_mismatch",
                                response={
                                    "code": "settings_write_mismatch",
                                    "revision": revision,
                                },
                                status_code=503,
                                now=utcnow_fn(),
                            )
                        except Exception:
                            pass
                        return jsonify(
                            {
                                "code": "settings_write_mismatch",
                                "revision": revision,
                            }
                        ), 503
                    try:
                        store.complete_settings_publication(
                            str(intent["id"]),
                            private_reference=actual_reference,
                            response=result,
                            now=utcnow_fn(),
                        )
                    except Exception:
                        return jsonify(
                            {
                                "code": "settings_reconcile_pending",
                                "revision": revision,
                            }
                        ), 202
                return jsonify(result)
        except ManagementIdempotencyConflictError:
            return jsonify({"code": "idempotency_conflict"}), 409
        except ValueError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "invalid_settings_request"},
                status_code=400,
            )
            return jsonify({"code": "invalid_settings_request"}), 400
        except Exception:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "settings_unavailable"},
                status_code=503,
            )
            return jsonify({"code": "settings_unavailable"}), 503

    @blueprint.post("/api/selector-probe/webhook-test")
    @allow_roles("administrator", "operator")
    def webhook_test_route():
        actor_id: int | None = None
        key = ""
        digest = ""
        operation = "webhook-test"
        try:
            payload = _strict_object(
                request.get_json(silent=True),
                required={"idempotency_key", "payload"},
            )
            key = _request_text(
                payload["idempotency_key"],
                "idempotency_key",
                maximum=128,
            )
            projected = _selector_settings_projection(
                settings_provider(), revision=settings_revision()
            )
            expected_payload = {
                "event": "selector_probe.webhook_test",
                "environment": projected["environment"],
                "site": projected["site"],
                "synthetic": True,
            }
            if payload["payload"] != expected_payload:
                raise ValueError("invalid synthetic webhook payload")
            actor_id, actor_name = _management_actor()
            with _open_store(store_factory) as store:
                proposed_delivery = f"synthetic-{uuid.uuid4().hex}"
                reservation, digest = reserve_operation(
                    store,
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload=expected_payload,
                    pending_response={
                        "status": "accepted",
                        "delivery_id": proposed_delivery,
                    },
                )
                if not reservation["reserved"]:
                    return (
                        jsonify(reservation["response"]),
                        int(reservation["status_code"]),
                    )
                dispatched = webhook_test_dispatcher(expected_payload)
                result = (
                    dict(dispatched)
                    if isinstance(dispatched, Mapping)
                    else {"status": "accepted"}
                )
                result.pop("payload", None)
                result.setdefault("status", "accepted")
                store.record_management_audit(
                    actor_user_id=actor_id,
                    actor_username=actor_name,
                    event_type="webhook_test_requested",
                    target_type="webhook",
                    target_id="selector_probe",
                    details={"synthetic": True},
                )
                result.setdefault("delivery_id", proposed_delivery)
                store.complete_management_operation(
                    actor_user_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    payload_hash=digest,
                    response=result,
                    status_code=202,
                )
            return jsonify(result), 202
        except ManagementIdempotencyConflictError:
            return jsonify({"code": "idempotency_conflict"}), 409
        except ValueError:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "invalid_webhook_test"},
                status_code=400,
            )
            return jsonify({"code": "invalid_webhook_test"}), 400
        except Exception:
            finish_operation_failure(
                actor_user_id=actor_id,
                operation=operation,
                idempotency_key=key,
                payload_hash=digest,
                response={"code": "webhook_test_unavailable"},
                status_code=503,
            )
            return jsonify({"code": "webhook_test_unavailable"}), 503

    def release_run(owner: str) -> None:
        nonlocal run_owner, run_busy_until
        with run_lock:
            if owner == run_owner:
                run_owner = ""
                run_busy_until = 0.0

    @blueprint.post("/api/selector-probe/run-now")
    @allow_roles("administrator", "operator")
    def run_now_route():
        nonlocal run_owner, run_busy_until
        legacy = getattr(g, "management_user", None) is None
        raw_payload = request.get_json(silent=True)
        try:
            if legacy and raw_payload is None:
                payload = {"idempotency_key": uuid.uuid4().hex}
            else:
                payload = _strict_object(
                    raw_payload,
                    required={"idempotency_key"},
                    optional={"retry_of_run_id"},
                )
            key = _request_text(
                payload["idempotency_key"],
                "idempotency_key",
                maximum=128,
            )
            retry_of = payload.get("retry_of_run_id")
            if retry_of is not None and (
                isinstance(retry_of, bool)
                or not isinstance(retry_of, (int, str))
            ):
                raise ValueError("invalid retry run")
            actor_id, actor_name = _management_actor()
            if not legacy:
                proposed_id = uuid.uuid4().hex
                pending = {
                    "status": "accepted",
                    "request_id": proposed_id,
                    "run_id": proposed_id,
                }
                if retry_of is not None:
                    pending["retry_of_run_id"] = retry_of
                with _open_store(store_factory) as store:
                    reservation, operation_digest = reserve_operation(
                        store,
                        actor_user_id=actor_id,
                        operation="run-now",
                        idempotency_key=key,
                        payload={"retry_of_run_id": retry_of or ""},
                        pending_response=pending,
                    )
                    if not reservation["reserved"]:
                        return (
                            jsonify(reservation["response"]),
                            int(reservation["status_code"]),
                        )
                    active = store.active_management_run_request()
                    if active is not None:
                        active_id = str(active["id"])
                        result = {
                            "status": "accepted",
                            "request_id": active_id,
                            "run_id": active_id,
                            "deduplicated": True,
                        }
                        store.complete_management_operation(
                            actor_user_id=actor_id,
                            operation="run-now",
                            idempotency_key=key,
                            payload_hash=operation_digest,
                            response=result,
                            status_code=202,
                        )
                        return jsonify(result), 202
                request_id = proposed_id
            else:
                request_id = uuid.uuid4().hex
                operation_digest = ""
        except ManagementIdempotencyConflictError:
            return jsonify({"code": "idempotency_conflict"}), 409
        except ValueError:
            return jsonify({"code": "invalid_run_request"}), 400
        except Exception:
            if legacy:
                return jsonify({"error": "dispatcher_unavailable"}), 503
            return jsonify({"code": "run_service_unavailable"}), 503
        now = monotonic_fn()
        with run_lock:
            if run_owner and now < run_busy_until:
                if legacy:
                    return jsonify({"error": "probe_busy"}), 409
                busy_result = {
                    "status": "accepted",
                    "request_id": run_owner,
                    "run_id": run_owner,
                    "active_run_id": run_owner,
                    "deduplicated": True,
                }
                with _open_store(store_factory) as store:
                    store.complete_management_operation(
                        actor_user_id=actor_id,
                        operation="run-now",
                        idempotency_key=key,
                        payload_hash=operation_digest,
                        response=busy_result,
                        status_code=202,
                    )
                return jsonify(busy_result), 202
            run_owner = request_id
            run_busy_until = now + local_busy_ttl_seconds
        if not legacy:
            try:
                with _open_store(store_factory) as store:
                    created_request = store.create_management_run_request(
                        request_id,
                        actor_user_id=actor_id,
                        actor_username=actor_name,
                        retry_of_run_id=retry_of or "",
                    )
                    if created_request.get("deduplicated") is True:
                        active_id = str(created_request["id"])
                        release_run(request_id)
                        result = {
                            "status": "accepted",
                            "request_id": active_id,
                            "run_id": active_id,
                            "deduplicated": True,
                        }
                        store.complete_management_operation(
                            actor_user_id=actor_id,
                            operation="run-now",
                            idempotency_key=key,
                            payload_hash=operation_digest,
                            response=result,
                            status_code=202,
                        )
                        return jsonify(result), 202
            except Exception:
                release_run(request_id)
                try:
                    with _open_store(store_factory) as store:
                        store.complete_management_operation(
                            actor_user_id=actor_id,
                            operation="run-now",
                            idempotency_key=key,
                            payload_hash=operation_digest,
                            response={
                                "code": "run_service_unavailable"
                            },
                            status_code=503,
                            failed=True,
                        )
                except Exception:
                    pass
                return jsonify(
                    {"code": "run_service_unavailable"}
                ), 503
        try:
            accepted = run_dispatcher(
                request_id,
                lambda: release_run(request_id),
            )
        except Exception:
            release_run(request_id)
            failure_response = {
                "error" if legacy else "code": "dispatcher_unavailable"
            }
            if not legacy:
                try:
                    with _open_store(store_factory) as store:
                        store.fail_management_run_request(request_id)
                        store.complete_management_operation(
                            actor_user_id=actor_id,
                            operation="run-now",
                            idempotency_key=key,
                            payload_hash=operation_digest,
                            response=failure_response,
                            status_code=503,
                            failed=True,
                        )
                except Exception:
                    pass
            return jsonify(failure_response), 503
        if accepted is False or (
            isinstance(accepted, Mapping)
            and accepted.get("status") == "busy"
        ):
            release_run(request_id)
            if legacy:
                return jsonify({"error": "probe_busy"}), 409
            active_id = (
                accepted.get("active_run_id")
                if isinstance(accepted, Mapping)
                else ""
            )
            busy_result = {
                "status": "accepted",
                "request_id": active_id or request_id,
                "run_id": active_id or request_id,
                "active_run_id": active_id,
                "deduplicated": bool(active_id),
            }
            with _open_store(store_factory) as store:
                store.fail_management_run_request(
                    request_id,
                    "dispatch_busy",
                )
                store.complete_management_operation(
                    actor_user_id=actor_id,
                    operation="run-now",
                    idempotency_key=key,
                    payload_hash=operation_digest,
                    response=busy_result,
                    status_code=202,
                )
            return jsonify(busy_result), 202
        if not (
            isinstance(accepted, Mapping)
            and accepted.get("completion_managed") is True
        ):
            release_run(request_id)
        result = {
            "status": "accepted",
            "request_id": request_id,
            "run_id": request_id,
            "deduplicated": False,
        }
        if retry_of is not None:
            result["retry_of_run_id"] = retry_of
        if not legacy:
            try:
                with _open_store(store_factory) as store:
                    store.complete_management_operation(
                        actor_user_id=actor_id,
                        operation="run-now",
                        idempotency_key=key,
                        payload_hash=operation_digest,
                        response=result,
                        status_code=202,
                    )
                    store.record_management_audit(
                        actor_user_id=actor_id,
                        actor_username=actor_name,
                        event_type="probe_run_requested",
                        target_type="probe_run_request",
                        target_id=request_id,
                        details={
                            "retry_of_run_id": retry_of or "",
                        },
                    )
            except Exception:
                pass
        return jsonify(result), 202

    return blueprint


__all__ = [
    "RELEASE_RUN_NOW_LUA",
    "RedisRunDispatcher",
    "check_strategy_gate",
    "create_selector_probe_blueprint",
    "default_element_request_dispatcher",
    "default_gate_service_factory",
    "default_run_dispatcher",
    "default_registry_factory",
    "default_store_factory",
    "unavailable_run_dispatcher",
]
