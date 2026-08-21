"""Account and proxy-pool routes (migrated from gateway/app.py)."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from gateway.account_store import (
    account_summary,
    assign_proxy_session,
    get_assigned_proxy_sessions,
    get_buffer_account,
    get_next_account,
    save_buffer_account,
    update_account,
)
from gateway.buffer_discovery import discover_accounts, import_buffer_accounts
from gateway.proxy_pool import (
    parse_proxy_pool,
    proxy_pool_key,
    select_proxy_from_pool,
    summarize_proxy_pool,
)
from gateway.settings_store import load_settings


def create_routes_accounts_blueprint() -> Blueprint:
    bp = Blueprint("accounts", __name__)

    @bp.get("/api/proxy-pool/status")
    def proxy_pool_status():
        settings = load_settings()
        assigned_sessions = get_assigned_proxy_sessions(
            current_app.config["ACCOUNTS_DB_PATH"]
        )
        try:
            page = max(int(request.args.get("page", 1)), 1)
            page_size = min(max(int(request.args.get("page_size", 50)), 1), 200)
        except (TypeError, ValueError):
            page = 1
            page_size = 50
        return jsonify(
            summarize_proxy_pool(
                settings.get("proxy_pool", {}).get("items", []),
                assigned_sessions,
                page=page,
                page_size=page_size,
                search=request.args.get("search", ""),
            )
        )

    @bp.get("/api/accounts")
    def accounts_route():
        return jsonify(account_summary(current_app.config["ACCOUNTS_DB_PATH"]))

    @bp.post("/api/accounts/save")
    def save_account_route():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(
                {
                    "account": save_buffer_account(
                        current_app.config["ACCOUNTS_DB_PATH"],
                        payload,
                    ),
                    **account_summary(
                        current_app.config["ACCOUNTS_DB_PATH"]
                    ),
                }
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    @bp.get("/api/account/next")
    def next_account():
        account = get_next_account(
            current_app.config["ACCOUNTS_DB_PATH"]
        )

        if account is None:
            return jsonify({"error": "no available account"}), 404

        return jsonify(account)

    @bp.post("/api/account/update")
    def update_account_route():
        payload = request.get_json(silent=True) or {}
        ads_power_user_id = payload.get("ads_power_user_id")
        result = payload.get("result")

        if not ads_power_user_id or result not in {
            "success",
            "failed",
            "banned",
            "abnormal",
        }:
            return (
                jsonify(
                    {
                        "error": (
                            "ads_power_user_id and valid result are required"
                        )
                    }
                ),
                400,
            )

        updated_account = update_account(
            current_app.config["ACCOUNTS_DB_PATH"],
            ads_power_user_id,
            result,
        )

        if updated_account is None:
            return jsonify({"error": "account not found"}), 404

        return jsonify(updated_account)

    @bp.post("/api/accounts/discover")
    def discover_accounts_route():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(
                discover_accounts(
                    current_app.config["ACCOUNTS_DB_PATH"],
                    payload.get("accountId"),
                )
            )
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 400

    @bp.post("/api/accounts/import")
    def import_accounts_route():
        payload = request.get_json(silent=True) or {}
        accounts = payload.get("accounts") or []
        if payload.get("buffer_token"):
            accounts = [
                *accounts,
                {
                    "account_name": payload.get("account_name", ""),
                    "buffer_token": payload.get("buffer_token", ""),
                    "buffer_api": payload.get("buffer_api", ""),
                },
            ]
        try:
            return jsonify(
                import_buffer_accounts(
                    current_app.config["ACCOUNTS_DB_PATH"],
                    accounts=accounts,
                    raw_text=payload.get("raw_text", ""),
                )
            )
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 400

    @bp.post("/api/accounts/proxy")
    def assign_account_proxy_route():
        payload = request.get_json(silent=True) or {}
        account_id = payload.get("account_id")
        mode = payload.get("mode")

        if not account_id or mode not in {"auto", "manual"}:
            return (
                jsonify(
                    {"error": "account_id and valid mode are required"}
                ),
                400,
            )

        if mode == "auto":
            settings = load_settings()
            account = get_buffer_account(
                current_app.config["ACCOUNTS_DB_PATH"],
                account_id,
            )
            current_proxy_session = (
                account.get("proxy_session") if account else ""
            )
            proxy_pool = settings.get("proxy_pool", {}).get("items", [])
            pool_sessions = {proxy_pool_key(item) for item in proxy_pool}
            assigned_sessions = set(
                get_assigned_proxy_sessions(
                    current_app.config["ACCOUNTS_DB_PATH"]
                )
            )
            if current_proxy_session in pool_sessions:
                proxy_session = current_proxy_session
            else:
                available_proxies = [
                    item
                    for item in proxy_pool
                    if proxy_pool_key(item) not in assigned_sessions
                ]
                selected_proxy = select_proxy_from_pool(
                    available_proxies,
                    account_id,
                )
                if selected_proxy is None:
                    return (
                        jsonify({"error": "no available proxy in pool"}),
                        400,
                    )
                proxy_session = proxy_pool_key(selected_proxy)
        else:
            try:
                proxy_session = proxy_pool_key(
                    parse_proxy_pool(payload.get("proxy", ""))[0]
                )
            except (IndexError, ValueError) as error:
                return (
                    jsonify(
                        {"error": str(error) or "proxy is required"}
                    ),
                    400,
                )

        account = assign_proxy_session(
            current_app.config["ACCOUNTS_DB_PATH"],
            account_id,
            proxy_session,
        )
        if account is None:
            return jsonify({"error": "account not found"}), 404

        return jsonify(
            {
                "account": account,
                **account_summary(
                    current_app.config["ACCOUNTS_DB_PATH"]
                ),
            }
        )

    return bp
