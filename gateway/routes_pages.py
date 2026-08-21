"""Page and evidence routes (migrated from gateway/app.py)."""

from __future__ import annotations

import re
from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, render_template_string, request, send_from_directory, session

from gateway.page_templates import CONTROL_PAGE_HTML

bp = Blueprint("pages", __name__)


@bp.get("/")
def dashboard_page():
    panel = request.args.get("panel", "settings")
    active_nav = {
        "publish": "publishing",
        "publish-results": "publishing",
        "content": "publishing",
        "accounts": "accounts-windows",
        "browser": "accounts-windows",
        "settings": "system-settings",
        "proxy-config": "system-settings",
    }.get(panel, "system-settings")
    return render_template_string(
        CONTROL_PAGE_HTML,
        active_nav=active_nav,
        csrf_token=session["csrf_token"],
    )


@bp.get("/browser-v2")
def browser_v2_page():
    active_nav = {
        "elements": "page-elements",
        "history": "receipts",
    }.get(request.args.get("view"), "action-library")
    return render_template(
        "browser_v2.html",
        active_nav=active_nav,
        csrf_token=session["csrf_token"],
    )


@bp.get("/comment-campaigns")
def comment_campaign_page():
    return render_template(
        "comment_campaign.html",
        active_nav="comment-campaign",
        csrf_token=session["csrf_token"],
    )


@bp.get("/evidence/<filename>")
def execution_v2_evidence(filename: str):
    if re.fullmatch(r"[0-9a-f]{32}\.png", filename) is None:
        abort(404)
    return send_from_directory(
        current_app.config["EXECUTION_V2_EVIDENCE_DIR"],
        filename,
    )


@bp.get("/comment-campaign-evidence/<filename>")
def comment_campaign_evidence(filename: str):
    if re.fullmatch(r"[0-9a-f]{32}\.png", filename) is None:
        abort(404)
    evidence_dir = Path(
        current_app.config["COMMENT_CAMPAIGN_EVIDENCE_DIR"]
    ).resolve()
    candidate = evidence_dir / filename
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.resolve().parent != evidence_dir
    ):
        abort(404)
    response = send_from_directory(evidence_dir, filename)
    response.cache_control.no_store = True
    return response
