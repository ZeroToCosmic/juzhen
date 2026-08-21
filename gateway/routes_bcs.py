"""Business control system pages (migrated gateway side, BCS frontend)."""

from __future__ import annotations

from flask import Blueprint, render_template, session

bp = Blueprint("bcs", __name__)


@bp.get("/bcs")
def bcs_dashboard_page():
    return render_template(
        "bcs.html",
        active_nav="overview",
        panel="dashboard",
        csrf_token=session["csrf_token"],
    )


@bp.get("/bcs/devices")
def bcs_devices_page():
    return render_template(
        "bcs.html",
        active_nav="runtime",
        panel="devices",
        csrf_token=session["csrf_token"],
    )


@bp.get("/bcs/tasks")
def bcs_tasks_page():
    return render_template(
        "bcs.html",
        active_nav="task-execution",
        panel="tasks",
        csrf_token=session["csrf_token"],
    )
