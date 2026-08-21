"""Incremental operator console pages over existing Agent services."""

from __future__ import annotations

import os

from flask import Blueprint, current_app, jsonify, redirect, render_template, session, url_for

from agent.client import CentralClient, CentralError


bp = Blueprint("console", __name__, url_prefix="/console")


@bp.get("")
@bp.get("/")
def index():
    return redirect(url_for("console.overview"))


@bp.get("/overview")
def overview():
    return _render("console_overview.html", "overview")


@bp.get("/collection")
def collection():
    return _render("console_collection.html", "collection")


@bp.get("/collection-results")
def collection_results():
    return _render("console_collection_results.html", "collection-results")


@bp.get("/api/tasks")
def local_tasks():
    configured_device_id = (
        current_app.config["AGENT_DEVICE_ID"]
        if "AGENT_DEVICE_ID" in current_app.config
        else os.getenv("AGENT_DEVICE_ID", "")
    )
    device_id = str(configured_device_id or "").strip()
    if not device_id:
        return jsonify({"connected": False, "reason": "device_not_configured", "tasks": []})
    try:
        client = CentralClient(
            base_url=current_app.config.get("CENTRAL_BASE_URL"),
            tenant_id=current_app.config.get("AGENT_TENANT_ID"),
            device_id=device_id,
            timeout=current_app.config.get("CENTRAL_REQUEST_TIMEOUT_SECONDS"),
        )
        tasks = [
            {
                "id": item.get("subtask_id"),
                "task_id": item.get("task_id"),
                "task_type": "中控任务",
                "status": item.get("status"),
                "updated_at": item.get("lease_timeout_at"),
            }
            for item in client.pull_subtasks()
            if isinstance(item, dict)
        ]
    except CentralError:
        return jsonify(
            {"error": {"code": "central_unavailable", "message": "中控暂不可用。"}}
        ), 503
    return jsonify({"connected": True, "device_id": device_id, "tasks": tasks})


@bp.get("/tasks")
def tasks():
    return _render("console_tasks.html", "task-execution")


@bp.get("/actions")
def actions():
    return _render("console_actions.html", "action-library")


@bp.get("/actions/comment-trees")
def comment_trees():
    return _render("console_comment_trees.html", "action-library")


@bp.get("/actions/comment-campaigns/new")
def new_comment_campaign():
    return _render(
        "console_comment_campaign_create.html",
        "action-library",
    )


@bp.get("/actions/browser-strategies/new")
def new_browser_strategy():
    return _render(
        "console_browser_strategy_editor.html",
        "action-library",
        editor_mode="new",
        strategy_id=None,
    )


@bp.get("/actions/browser-strategies/<strategy_id>/edit")
def edit_browser_strategy(strategy_id: str):
    return _render(
        "console_browser_strategy_editor.html",
        "action-library",
        editor_mode="edit",
        strategy_id=strategy_id,
    )


@bp.get("/publishing")
def publishing():
    return _render("console_publishing.html", "publishing")


@bp.get("/accounts-windows")
def accounts_windows():
    return _render("console_accounts_windows.html", "accounts-windows")


@bp.get("/runtime")
def runtime():
    return redirect(url_for("console.overview", _anchor="local-runtime"))


@bp.get("/page-elements")
def page_elements():
    return _render("console_page_elements.html", "page-elements")


@bp.get("/receipts")
def receipts():
    return _render("console_receipts.html", "receipts")


@bp.get("/settings")
def settings():
    return _render("console_settings.html", "system-settings")


def _render(template: str, active_nav: str, **context):
    return render_template(
        template,
        active_nav=active_nav,
        csrf_token=session["csrf_token"],
        **context,
    )
