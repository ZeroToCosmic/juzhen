"""Health and status routes (migrated from gateway/app.py)."""

from __future__ import annotations

from flask import Blueprint, jsonify

from gateway.settings_store import load_settings


def create_health_blueprint() -> Blueprint:
    bp = Blueprint("health", __name__)

    @bp.get("/ping")
    def ping():
        return jsonify({"status": "ok"})

    @bp.get("/api/status")
    def get_status():
        settings = load_settings()
        proxy = settings["proxy"]
        proxy_pool = settings.get("proxy_pool", {})
        services = settings["services"]
        browser = settings["browser"]
        adspower = settings.get("adspower", {})
        single_proxy_configured = all(
            proxy.get(key)
            for key in ("host", "port", "username", "password")
        )
        proxy_pool_configured = bool(proxy_pool.get("items"))

        return jsonify(
            {
                "service": {"running": True},
                "config": {
                    "proxy_configured": single_proxy_configured
                    or proxy_pool_configured,
                    "services_configured": bool(
                        services.get("ipinfo_url")
                        and services.get("buffer_graphql_url")
                    ),
                    "browser_configured": bool(
                        browser.get("cdp_url")
                        or (
                            adspower.get("base_url")
                            and browser.get("default_url")
                        )
                    ),
                },
                "browser": {
                    "cdp_url": browser.get("cdp_url", ""),
                    "task_goal": browser.get("task_goal", ""),
                },
            }
        )

    return bp
