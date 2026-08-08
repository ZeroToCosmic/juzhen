import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
import re
import random
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests
from pathlib import Path
from flask import Flask, abort, g, jsonify, render_template, render_template_string, request, send_from_directory, session
from werkzeug.exceptions import RequestEntityTooLarge

from adspower import AdsPowerController, AdsPowerError
from browser_public_identity import mask_profile_id

LOGGER = logging.getLogger(__name__)

from gateway.account_store import (
    account_summary,
    assign_proxy_session,
    get_assigned_proxy_sessions,
    get_buffer_account,
    get_next_account,
    save_buffer_account,
    update_account,
)
from gateway.auth_blueprint import (
    create_auth_blueprint,
    install_management_guard,
)
from gateway.auth_service import AuthService
from gateway.auth_store import AuthStore
from gateway.buffer_client import (
    extract_tiktok_url_from_buffer_payload,
    fetch_buffer_post,
    publish_to_buffer,
)
from gateway.buffer_discovery import discover_accounts, import_buffer_accounts
from gateway.config import load_proxy_config
from gateway.content_import import parse_copy_import
from gateway.content_store import (
    DEFAULT_CONTENT_DIR,
    add_copy_item,
    apply_copy_import,
    cleanup_publish_logs,
    compose_text,
    create_brand,
    delete_batch_publish_run,
    delete_daily_schedule,
    get_copy_item,
    get_video,
    list_batch_publish_runs,
    list_brands,
    list_copy_items,
    list_daily_schedules,
    mark_publish_sample_failure,
    mark_publish_sample_success,
    mark_tiktok_link_backfill_failure,
    mark_tiktok_link_backfill_success,
    mark_video_used,
    next_due_publish_sample,
    next_pending_publish_task,
    next_pending_tiktok_link_backfill,
    now_iso as content_now_iso,
    publish_stats,
    public_publish_tasks,
    rename_brand,
    save_batch_publish_run,
    save_daily_schedule,
    save_publish_task,
    sync_video_library,
    update_batch_publish_run,
    update_daily_schedule,
    update_publish_task,
    update_publish_metrics,
    unused_videos,
    video_summary,
)
from gateway.ip_checker import fetch_ip_info
from gateway.model_presets import public_model_presets
from gateway.management_db import open_management_db
from gateway.local_only import install_local_only_guard
from gateway.page_templates import (
    CONTROL_PAGE_HTML,
    DASHBOARD_PAGE_HTML,
    SETTINGS_PAGE_HTML,
)
from gateway.proxy import build_static_proxy_url, generate_proxy_url
from gateway.proxy_pool import parse_proxy_pool, proxy_pool_key, select_proxy_from_pool, summarize_proxy_pool
from gateway.publish_queue import (
    buffer_post_id_for_task,
    build_proxy_url_from_session,
    build_tiktok_sampler_command,
    create_publish_task,
    enrich_publish_stats_with_accounts,
    execute_next_publish_sample,
    execute_next_publish_task,
    execute_next_tiktok_link_backfill,
    execute_publish_sampling_tick,
    get_proxy_url_for_account,
    publish_sampling_options,
    select_publish_accounts,
    start_publish_queue_worker,
    start_publish_sampling_worker,
    tiktok_username_from_url,
)
from gateway.r2_client import list_r2_video_objects
from gateway.routes_accounts import create_routes_accounts_blueprint
from gateway.routes_health import create_health_blueprint
from gateway.routes_ip import create_routes_ip_blueprint
from gateway.routes_pages import bp as pages_blueprint
from gateway.routes_publish import create_routes_publish_blueprint
from gateway.routes_settings import create_settings_blueprint
from gateway.routes_browser import bp as legacy_browser_blueprint
from gateway.browser_legacy import (
    _dependency_aware_gate_factory,
    ACTIVE_BROWSER_SESSIONS,
    ACTIVE_BROWSER_SESSIONS_LOCK,
    ACTIVE_PATTERN_RECORDINGS,
    ACTIVE_PATTERN_RECORDINGS_LOCK,
    BROWSER_BATCH_TASKS,
    BROWSER_BATCH_TASKS_LOCK,
    BROWSER_LOG_LOCK,
    BROWSER_LOG_PATH,
    BROWSER_PROFILE_EXECUTIONS,
    BROWSER_PROFILE_EXECUTIONS_LOCK,
    BROWSER_PROFILE_SESSION_LOCKS,
    BROWSER_PROFILE_SESSION_LOCKS_LOCK,
    BROWSER_SESSION_LEASES,
    BrowserExecutionBusyError,
    BrowserStageError,
    PROJECT_ROOT,
    PUBLIC_BROWSER_ASSIGNMENT_PATTERN,
    PUBLIC_BROWSER_HEADER_PATTERN,
    PUBLIC_BROWSER_SPACE_ASSIGNMENT_PATTERN,
    PUBLIC_CREDENTIAL_VALUE_PATTERN,
    SAFE_PUBLIC_CREDENTIAL_STATUSES,
    SAFE_PUBLIC_DIAGNOSTIC_KEYS,
    SENSITIVE_BROWSER_KEY_MARKERS,
    acquire_browser_session_use,
    browser_profile_execution_reservation,
    browser_profile_session_lock,
    browser_strategy_gate_check,
    browser_tile_error,
    build_direct_agent_command,
    build_execution_v2_content_library_provider,
    build_execution_v2_text_resolver,
    build_search_agent_command,
    build_strategy_generation_prompt,
    build_strategy_text_resolver,
    collect_strategy_comments,
    ensure_browser_profile_sessions,
    extract_model_text,
    fetch_adspower_windows,
    generate_execution_strategies,
    get_adspower_base_url,
    get_adspower_headers,
    get_async_playwright,
    get_browser_target_url,
    inspect_browser_elements_on_cdp,
    is_safe_public_credential_value,
    is_safe_public_header_value,
    is_sensitive_browser_key,
    is_sensitive_browser_payload_key,
    is_valid_browser_url,
    load_persisted_strategy_state,
    mutate_persisted_strategy_state,
    normalize_execution_strategies,
    normalize_execution_strategy,
    normalize_selected_browser_profiles,
    normalize_sensitive_browser_key,
    parse_strategy_json_from_text,
    prepare_browser_page,
    public_browser_batch_result,
    public_browser_batch_task,
    public_browser_payload,
    public_element_inspection,
    public_strategy_action_result,
    public_strategy_execution_result,
    public_strategy_failure_result,
    record_browser_log,
    redact_public_browser_credential,
    release_browser_session_results,
    release_browser_session_use,
    release_selected_browser_sessions,
    request_model_text,
    run_browser_batch_task,
    sanitize_adspower_profile,
    sanitize_browser_log_file,
    sanitize_public_browser_assignments,
    sanitize_public_browser_fragment,
    sanitize_public_browser_headers,
    sanitize_public_browser_origin,
    sanitize_public_browser_text,
    sanitize_public_browser_url,
    save_execution_strategies,
    select_model_for_generation,
    selected_browser_sessions,
    strategy_comment_texts,
    update_browser_batch_task,
)
from gateway.settings_store import (
    get_config_health,
    load_settings,
    public_settings,
    restore_latest_backup_preserving,
    save_settings,
    mutate_settings,
    update_settings as merge_saved_settings,
)
from gateway.session_key import load_or_create_session_key
from execution_v2.blueprint import create_browser_v2_blueprint
from comment_campaign.blueprint import create_comment_campaign_blueprint
from browser_strategy_config import (
    ACTION_CATALOG,
    DEFAULT_ACTION_PARAMS,
    element_references,
    load_or_migrate_strategy_state,
    normalize_block_strategies,
    normalize_elements,
    normalize_patterns,
    pattern_references,
)
from browser_element_resolver import LocatorResolutionError, inspect_element
from browser_element_schema import TIKTOK_COMMENT_TEMPLATE, normalize_element_definitions
from tiktok_stats.blueprint import (
    create_tiktok_stats_blueprint,
    default_query_factory,
    default_secret_store_factory,
    default_status_provider,
    default_store_factory,
    register_tiktok_stats_error_handler,
    unavailable_cookie_validator,
    unavailable_run_dispatcher,
)
from selector_probe.blueprint import (
    check_strategy_gate,
    create_selector_probe_blueprint,
    default_gate_service_factory as default_selector_probe_gate_service_factory,
    default_registry_factory as default_selector_probe_registry_factory,
    default_run_dispatcher as default_selector_probe_dispatcher,
    default_store_factory as default_selector_probe_store_factory,
)






def _local_direct_mode_enabled(config: dict | None) -> bool:
    """Config wins; environment accepts only explicit local-direct values."""

    if config is not None and "LOCAL_DIRECT_MODE" in config:
        value = config["LOCAL_DIRECT_MODE"]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return False
    return os.getenv("LOCAL_DIRECT_MODE", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    local_direct_mode = _local_direct_mode_enabled(config)
    explicit_gate_factory = bool(
        config and "SELECTOR_PROBE_GATE_SERVICE_FACTORY" in config
    )
    app.config["PROXY_CONFIG"] = load_proxy_config()
    app.config.setdefault("ACCOUNTS_DB_PATH", "accounts.db")
    app.config.setdefault("CONTENT_DATA_DIR", DEFAULT_CONTENT_DIR)
    app.config.setdefault("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)
    stats_root = Path(__file__).resolve().parents[1] / "data" / "stats"
    app.config.setdefault("TIKTOK_STATS_DB_PATH", stats_root / "tiktok_stats.db")
    app.config.setdefault("TIKTOK_STATS_COOKIE_PATH", stats_root / "tiktok_cookie.json")
    app.config.setdefault("TIKTOK_STATS_QUERY_FACTORY", default_query_factory)
    app.config.setdefault("TIKTOK_STATS_STORE_FACTORY", default_store_factory)
    app.config.setdefault("TIKTOK_STATS_SECRET_STORE_FACTORY", default_secret_store_factory)
    app.config.setdefault("TIKTOK_STATS_COOKIE_VALIDATOR", unavailable_cookie_validator)
    app.config.setdefault("TIKTOK_STATS_RUN_DISPATCHER", unavailable_run_dispatcher)
    app.config.setdefault("TIKTOK_STATS_STATUS_PROVIDER", default_status_provider)
    app.config.setdefault(
        "SELECTOR_PROBE_STORE_FACTORY",
        default_selector_probe_store_factory,
    )
    app.config.setdefault(
        "SELECTOR_PROBE_REGISTRY_FACTORY",
        default_selector_probe_registry_factory,
    )
    app.config.setdefault(
        "SELECTOR_PROBE_RUN_DISPATCHER",
        default_selector_probe_dispatcher,
    )
    if config:
        app.config.update(config)
    app.config["LOCAL_DIRECT_MODE"] = local_direct_mode
    app.config.setdefault("SERVER_PORT", 5000)
    app.config.setdefault(
        "EXECUTION_V2_DB_PATH",
        Path(__file__).resolve().parents[1]
        / "data"
        / "execution_v2"
        / "execution_v2.db",
    )
    app.config.setdefault(
        "EXECUTION_V2_EVIDENCE_DIR",
        Path(__file__).resolve().parents[1] / "data" / "execution_v2" / "evidence",
    )
    app.config.setdefault("EXECUTION_V2_SERVICE_FACTORY", None)
    app.config.setdefault(
        "COMMENT_CAMPAIGN_DB_URL",
        "sqlite:///data/comment_campaign/comment_campaign.db",
    )
    app.config.setdefault(
        "COMMENT_CAMPAIGN_EVIDENCE_DIR",
        "data/comment_campaign/evidence",
    )
    app.config.setdefault(
        "COMMENT_CAMPAIGN_REDIS_URL",
        os.getenv(
            "COMMENT_CAMPAIGN_REDIS_URL",
            os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0"),
        ),
    )
    app.config.setdefault("COMMENT_CAMPAIGN_SERVICE_FACTORY", None)
    if not local_direct_mode:
        app.config.setdefault("MANAGEMENT_STATE_DIR", Path("data"))
        app.config.setdefault(
            "MANAGEMENT_DB_PATH",
            Path(app.config["MANAGEMENT_STATE_DIR"]) / "management.db",
        )
    app.config.update(
        SECRET_KEY=(
            secrets.token_urlsafe(48)
            if local_direct_mode
            else load_or_create_session_key(
                Path(app.config["MANAGEMENT_STATE_DIR"]) / "session.key"
            )
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(
            app.config.get("PUBLIC_ORIGIN_HTTPS", False)
        ),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    app.config.setdefault(
        "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
        set(),
    )
    if explicit_gate_factory:
        base_gate_factory = app.config[
            "SELECTOR_PROBE_GATE_SERVICE_FACTORY"
        ]
    else:
        selector_probe_store_factory = app.config[
            "SELECTOR_PROBE_STORE_FACTORY"
        ]

        def base_gate_factory():
            return default_selector_probe_gate_service_factory(
                store_factory=selector_probe_store_factory,
            )

    app.config["SELECTOR_PROBE_GATE_SERVICE_FACTORY"] = (
        _dependency_aware_gate_factory(base_gate_factory)
    )
    app.config.setdefault(
        "TIKTOK_STATS_EXISTING_ACCOUNTS_DB_PATH", app.config["ACCOUNTS_DB_PATH"]
    )

    if not local_direct_mode:
        def management_auth_service_factory():
            service = g.get("management_auth_service")
            if service is None:
                connection = open_management_db(
                    Path(app.config["MANAGEMENT_DB_PATH"])
                )
                g.management_auth_connection = connection
                service = AuthService(AuthStore(connection))
                g.management_auth_service = service
            return service

        @app.teardown_appcontext
        def close_management_auth_connection(_error):
            connection = g.pop("management_auth_connection", None)
            g.pop("management_auth_service", None)
            if connection is not None:
                connection.close()

        app.extensions["management_auth_service_factory"] = (
            management_auth_service_factory
        )
    register_tiktok_stats_error_handler(app)
    if local_direct_mode:
        install_local_only_guard(app)

        @app.before_request
        def ensure_local_direct_csrf():
            csrf_token = session.get("csrf_token")
            if not isinstance(csrf_token, str) or not csrf_token:
                session["csrf_token"] = secrets.token_urlsafe(32)
    else:
        app.register_blueprint(
            create_auth_blueprint(management_auth_service_factory)
        )
        install_management_guard(app, management_auth_service_factory)

    execution_v2_lock = threading.Lock()

    def execution_v2_service_factory():
        service = app.extensions.get("execution_v2_service")
        if service is None:
            with execution_v2_lock:
                service = app.extensions.get("execution_v2_service")
                if service is None:
                    configured_factory = app.config["EXECUTION_V2_SERVICE_FACTORY"]
                    if configured_factory is None:
                        from execution_v2.service import create_default_execution_v2_service

                        adspower_settings = load_settings().get("adspower", {})
                        controller = AdsPowerController(
                            base_url=(
                                adspower_settings.get("base_url")
                                or os.getenv("ADSPOWER_BASE_URL")
                            ),
                            api_key=(
                                adspower_settings.get("api_key")
                                or os.getenv("ADSPOWER_API_KEY", "")
                            ),
                        )
                        service = create_default_execution_v2_service(
                            db_path=app.config["EXECUTION_V2_DB_PATH"],
                            evidence_dir=app.config["EXECUTION_V2_EVIDENCE_DIR"],
                            controller=controller,
                            content_library_provider=(
                                build_execution_v2_content_library_provider(
                                    app.config["CONTENT_DATA_DIR"]
                                )
                            ),
                            text_resolver=build_execution_v2_text_resolver(
                                app.config["CONTENT_DATA_DIR"]
                            ),
                        )
                    else:
                        service = configured_factory()
                    app.extensions["execution_v2_service"] = service
        return service

    def close_execution_v2_service():
        with execution_v2_lock:
            service = app.extensions.pop("execution_v2_service", None)
            close = getattr(service, "close", None)
            if callable(close):
                close()

    app.extensions["execution_v2_service_factory"] = execution_v2_service_factory
    app.extensions["execution_v2_close"] = close_execution_v2_service
    app.register_blueprint(create_browser_v2_blueprint(execution_v2_service_factory))

    comment_campaign_lock = threading.Lock()

    def comment_campaign_service_factory():
        service = app.extensions.get("comment_campaign_service")
        if service is None:
            with comment_campaign_lock:
                service = app.extensions.get("comment_campaign_service")
                if service is None:
                    configured_factory = app.config["COMMENT_CAMPAIGN_SERVICE_FACTORY"]
                    if configured_factory is None:
                        from comment_campaign.service import (
                            create_default_comment_campaign_service,
                        )
                        from comment_campaign.queueing import QueueCoordinator

                        adspower_settings = load_settings().get("adspower", {})
                        controller = AdsPowerController(
                            base_url=(
                                adspower_settings.get("base_url")
                                or os.getenv("ADSPOWER_BASE_URL")
                            ),
                            api_key=(
                                adspower_settings.get("api_key")
                                or os.getenv("ADSPOWER_API_KEY", "")
                            ),
                        )
                        health_controller = AdsPowerController(
                            base_url=(
                                adspower_settings.get("base_url")
                                or os.getenv("ADSPOWER_BASE_URL")
                            ),
                            api_key=(
                                adspower_settings.get("api_key")
                                or os.getenv("ADSPOWER_API_KEY", "")
                            ),
                            timeout=1.0,
                            max_retries=1,
                            retry_delay=0,
                        )

                        def adspower_probe():
                            list_one = getattr(health_controller, "list_profiles", None)
                            if callable(list_one):
                                list_one(page=1, page_size=1)
                                return []
                            list_all = getattr(
                                health_controller, "list_all_profiles", None
                            )
                            if not callable(list_all):
                                raise RuntimeError("adspower probe unavailable")
                            try:
                                list_all(max_profiles=1)
                            except TypeError:
                                list_all()
                            return []

                        def profile_provider():
                            list_all = getattr(controller, "list_all_profiles", None)
                            if callable(list_all):
                                profiles = list_all()
                            else:
                                profiles = controller.list_profiles()
                            return [
                                {
                                    "id": str(item.get("id") or ""),
                                    "name": str(item.get("name") or ""),
                                    "status": str(item.get("status") or ""),
                                }
                                for item in profiles
                                if isinstance(item, dict)
                            ]

                        def content_resolver(library_id):
                            return [
                                {
                                    "content_item_id": str(item.get("id") or ""),
                                    "text": compose_text(item),
                                }
                                for item in list_copy_items(
                                    app.config["CONTENT_DATA_DIR"], library_id
                                )
                                if isinstance(item, dict)
                            ]

                        def publish_result_resolver(reference):
                            for item in public_publish_tasks(
                                app.config["CONTENT_DATA_DIR"]
                            ):
                                if (
                                    str(item.get("id") or "") == reference
                                    and item.get("status") == "success"
                                    and str(item.get("tiktok_url") or "").strip()
                                ):
                                    return str(item.get("tiktok_url") or "")
                            return ""

                        def comment_settings_provider():
                            settings = load_settings()
                            campaign = settings.get("comment_campaign", {})
                            return campaign if isinstance(campaign, dict) else {}

                        def comment_settings_updater(expected_revision, bindings):
                            from comment_campaign.errors import RevisionConflictError
                            from comment_campaign.errors import CampaignValidationError
                            from execution_v2.store import ExecutionStore

                            required_kinds = {
                                "entry_element_id": "click",
                                "input_element_id": "input",
                                "submit_element_id": "click",
                                "account_element_id": "click",
                            }
                            element_store = ExecutionStore(
                                app.config["EXECUTION_V2_DB_PATH"]
                            )
                            try:
                                element_store.initialize()
                                for name, kind in required_kinds.items():
                                    element = element_store.get_element(bindings[name])
                                    if (
                                        not isinstance(element, dict)
                                        or element.get("status") != "active"
                                        or element.get("kind") != kind
                                    ):
                                        raise CampaignValidationError("comment_panel_not_ready")
                            finally:
                                # ExecutionStore currently opens short-lived SQLite sessions per
                                # call.  Keep this defensive close so a future persistent store
                                # implementation cannot leak from a settings request.
                                close = getattr(element_store, "close", None)
                                if callable(close):
                                    close()

                            result = {}

                            def persist(settings):
                                campaign = settings.get("comment_campaign", {})
                                campaign = dict(campaign) if isinstance(campaign, dict) else {}
                                current = campaign.get("revision", 1)
                                current = current if type(current) is int and current >= 1 else 1
                                if current != expected_revision:
                                    raise RevisionConflictError("comment-settings")
                                campaign["element_bindings"] = dict(bindings)
                                campaign["revision"] = current + 1
                                settings["comment_campaign"] = campaign
                                result.update(campaign)
                                return settings

                            mutate_settings(persist)
                            return result

                        service = create_default_comment_campaign_service(
                            database_url=app.config["COMMENT_CAMPAIGN_DB_URL"],
                            profile_provider=profile_provider,
                            content_resolver=content_resolver,
                            publish_result_resolver=publish_result_resolver,
                            queue_coordinator=QueueCoordinator.from_url(
                                app.config["COMMENT_CAMPAIGN_REDIS_URL"]
                            ),
                            settings_provider=comment_settings_provider,
                            adspower_probe=adspower_probe,
                            settings_updater=comment_settings_updater,
                        )
                    else:
                        service = configured_factory()
                    app.extensions["comment_campaign_service"] = service
        return service

    def close_comment_campaign_service():
        with comment_campaign_lock:
            service = app.extensions.pop("comment_campaign_service", None)
            close = getattr(service, "close", None)
            if callable(close):
                close()

    app.extensions["comment_campaign_service_factory"] = (
        comment_campaign_service_factory
    )
    app.extensions["comment_campaign_close"] = close_comment_campaign_service
    app.register_blueprint(
        create_comment_campaign_blueprint(comment_campaign_service_factory)
    )
    app.register_blueprint(create_tiktok_stats_blueprint())
    app.register_blueprint(
        create_selector_probe_blueprint(
            store_factory=app.config["SELECTOR_PROBE_STORE_FACTORY"],
            registry_factory=app.config[
                "SELECTOR_PROBE_REGISTRY_FACTORY"
            ],
            gate_service_factory=app.config[
                "SELECTOR_PROBE_GATE_SERVICE_FACTORY"
            ],
            run_dispatcher=app.config[
                "SELECTOR_PROBE_RUN_DISPATCHER"
            ],
        )
    )
    app.register_blueprint(create_health_blueprint())
    app.register_blueprint(create_settings_blueprint())
    app.register_blueprint(create_routes_accounts_blueprint())
    app.register_blueprint(create_routes_ip_blueprint(get_proxy_url_for_account))
    app.register_blueprint(create_routes_publish_blueprint())
    app.register_blueprint(legacy_browser_blueprint)
    app.register_blueprint(pages_blueprint)
    sanitize_browser_log_file()

    @app.after_request
    def sanitize_browser_api_response(response):
        if (
            request.path.startswith("/api/browser/")
            and request.path != "/api/browser/adspower-windows"
            and response.is_json
        ):
            payload = response.get_json(silent=True)
            if payload is not None:
                response.set_data(app.json.dumps(public_browser_payload(payload)))
                response.mimetype = "application/json"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        return jsonify({"error": "导入文件不能超过 10 MB"}), 413

    if os.getenv("PUBLISH_WORKER_ENABLED") == "1":
        start_publish_queue_worker(app)
    start_publish_sampling_worker(app)

    return app