"""IP check and Buffer publish routes (migrated from gateway/app.py)."""

from __future__ import annotations

from typing import Callable

import requests
from flask import Blueprint, current_app, jsonify, request

from gateway.account_store import get_buffer_account
from gateway.buffer_client import publish_to_buffer
from gateway.ip_checker import fetch_ip_info


def create_routes_ip_blueprint(
    get_proxy_url_for_account: Callable[[str, str], str | None],
) -> Blueprint:
    bp = Blueprint("ip", __name__)

    @bp.post("/check_ip")
    def check_ip():
        payload = request.get_json(silent=True) or {}
        account_id = payload.get("account_id")

        if not account_id:
            return jsonify({"error": "account_id is required"}), 400

        proxy_url = get_proxy_url_for_account(
            current_app.config["ACCOUNTS_DB_PATH"],
            account_id,
        )

        try:
            return jsonify(fetch_ip_info(proxy_url))
        except requests.RequestException:
            return (
                jsonify({"error": "failed to fetch ip info through proxy"}),
                502,
            )

    @bp.post("/publish/buffer")
    def publish_buffer():
        request_payload = request.get_json(silent=True) or {}
        account_id = request_payload.get("account_id")
        access_token = request_payload.get("access_token")
        payload = request_payload.get("payload")

        if not account_id or payload is None:
            return (
                jsonify(
                    {
                        "error": (
                            "account_id, access_token, and payload are required"
                        )
                    }
                ),
                400,
            )

        account = get_buffer_account(
            current_app.config["ACCOUNTS_DB_PATH"],
            account_id,
        )
        if account:
            access_token = account.get("buffer_token") or access_token
            profile_ids = account.get("buffer_profile_ids") or []
            if profile_ids:
                payload = {**payload, "profile_ids": profile_ids}

        if not access_token:
            return (
                jsonify(
                    {"error": "access_token is required for this account"}
                ),
                400,
            )

        proxy_url = get_proxy_url_for_account(
            current_app.config["ACCOUNTS_DB_PATH"],
            account_id,
        )

        try:
            return jsonify(publish_to_buffer(proxy_url, access_token, payload))
        except requests.exceptions.RequestException as error:
            return jsonify({"error": str(error)}), 502

    return bp
