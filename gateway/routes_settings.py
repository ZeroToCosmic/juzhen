"""Settings routes (migrated from gateway/app.py)."""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template_string, request, session

from gateway.model_presets import public_model_presets
from gateway.page_templates import SETTINGS_PAGE_HTML
from gateway.settings_store import (
    get_config_health,
    load_settings,
    public_settings,
    restore_latest_backup_preserving,
    update_settings as merge_saved_settings,
)
from selector_probe.blueprint import (
    default_store_factory as default_selector_probe_store_factory,
)


def create_settings_blueprint() -> Blueprint:
    bp = Blueprint("settings", __name__)

    @bp.get("/settings")
    def settings_page():
        return render_template_string(
            SETTINGS_PAGE_HTML,
            csrf_token=session["csrf_token"],
        )

    @bp.get("/api/settings")
    def get_settings():
        return jsonify(public_settings(load_settings()))

    @bp.get("/api/model-presets")
    def get_model_presets():
        return jsonify(public_model_presets())

    @bp.get("/api/settings/status")
    def get_settings_status():
        return jsonify(get_config_health())

    @bp.post("/api/settings/restore-latest")
    def restore_latest_settings():
        try:
            settings = restore_latest_backup_preserving(
                ("selector_probe", "models", "adspower")
            )
        except FileNotFoundError as error:
            return jsonify({"error": str(error)}), 404
        return jsonify(
            {
                "settings": public_settings(settings),
                "status": get_config_health(),
            }
        )

    @bp.route("/api/settings", methods=["PUT", "POST"])
    def update_settings():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify(
                {"error": "settings payload must be a JSON object"}
            ), 400
        payload.pop("_secrets_configured", None)
        if "selector_probe" in payload:
            return jsonify(
                {
                    "code": "selector_probe_settings_managed",
                    "error": (
                        "selector_probe settings must be changed through "
                        "the selector-probe API"
                    ),
                }
            ), 409
        shared_probe_fields = {"models", "adspower"} & set(payload)
        current_probe = load_settings().get("selector_probe", {})
        if (
            shared_probe_fields
            and isinstance(current_probe, dict)
            and current_probe.get("enabled") is True
        ):
            return jsonify(
                {
                    "code": "selector_probe_settings_managed",
                    "error": (
                        "models and adspower settings used by an enabled "
                        "selector probe require the selector-probe API"
                    ),
                }
            ), 409
        try:
            if shared_probe_fields:
                with default_selector_probe_store_factory() as store:
                    store.bump_resource_revision("settings")
            updated = merge_saved_settings(payload)
            return jsonify(public_settings(updated))
        except ValueError as error:
            status_code = 409 if "配置文件无法读取" in str(error) else 400
            return jsonify({"error": str(error)}), status_code

    return bp
