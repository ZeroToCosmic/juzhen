"""Publish queue business logic (migrated from gateway/app.py)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

from gateway.account_store import account_summary, get_buffer_account
from gateway.buffer_client import (
    extract_tiktok_url_from_buffer_payload,
    fetch_buffer_post,
    publish_to_buffer,
)
from gateway.content_store import (
    cleanup_publish_logs,
    compose_text,
    delete_batch_publish_run,
    delete_daily_schedule,
    get_copy_item,
    get_video,
    list_batch_publish_runs,
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
    public_publish_tasks,
    publish_stats,
    save_batch_publish_run,
    save_daily_schedule,
    save_publish_task,
    update_batch_publish_run,
    update_daily_schedule,
    update_publish_metrics,
    update_publish_task,
    unused_videos,
)
from gateway.proxy import build_static_proxy_url, generate_proxy_url
from gateway.proxy_pool import parse_proxy_pool
from gateway.settings_store import load_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def build_proxy_url_from_session(account_id, proxy_session):
    items = parse_proxy_pool(proxy_session)
    protocol = load_settings().get("proxy_pool", {}).get("protocol", "socks5")
    return build_static_proxy_url(items[0], protocol)


def get_proxy_url_for_account(db_path, account_id):
    account = get_buffer_account(db_path, account_id)
    proxy_session = account.get("proxy_session") if account else ""
    if proxy_session:
        try:
            return build_proxy_url_from_session(account_id, proxy_session)
        except ValueError:
            pass

    return generate_proxy_url(account_id)


def create_publish_task(
    *,
    data_dir,
    db_path,
    account_id,
    profile_id,
    video_id,
    brand_id,
    copy_id=None,
    scheduled_at="",
    publish_now=True,
):
    account = get_buffer_account(db_path, account_id)
    video = get_video(data_dir, video_id)
    copy_item = get_copy_item(data_dir, brand_id, copy_id)
    if account is None:
        raise ValueError("account not found")
    if video is None or video.get("used"):
        raise ValueError("video is not available")
    if copy_item is None:
        raise ValueError("copy item not found")

    text = compose_text(copy_item)
    payload = {
        "text": text,
        "profile_ids": [profile_id],
        "media": {"link": video.get("url", "")},
    }
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at

    proxy_url = get_proxy_url_for_account(db_path, account_id)
    task = {
        "account_id": account_id,
        "profile_id": profile_id,
        "video_id": video_id,
        "video_url": video.get("url", ""),
        "brand_id": brand_id,
        "copy_id": copy_item.get("id", ""),
        "scheduled_at": scheduled_at,
        "proxy_session": account.get("proxy_session", ""),
        "copy_text": text,
        "status": "pending",
        "views": 0,
        "comments": 0,
    }

    if not publish_now:
        mark_video_used(data_dir, video_id)
        return save_publish_task(data_dir, task)

    try:
        result = publish_to_buffer(proxy_url, account.get("buffer_token", ""), payload)
        task["status"] = "success" if result.get("success", True) else "failed"
        task["buffer_response"] = result
        task["buffer_update_id"] = result.get("update_id", "")
        task["tiktok_url"] = result.get("tiktok_url", "")
    except requests.exceptions.RequestException as error:
        task["status"] = "failed"
        task["error"] = str(error)

    if task["status"] == "success":
        mark_video_used(data_dir, video_id)
    return save_publish_task(data_dir, task)


def execute_next_publish_task(data_dir, db_path):
    task = next_pending_publish_task(data_dir)
    if task is None:
        return {"processed": False, "task": None}

    update_publish_task(
        data_dir,
        task["id"],
        {"status": "processing", "started_at": content_now_iso()},
    )
    account = get_buffer_account(db_path, task.get("account_id", ""))
    if account is None:
        updated = update_publish_task(
            data_dir,
            task["id"],
            {
                "status": "failed",
                "error": "account not found",
                "finished_at": content_now_iso(),
            },
        )
        return {"processed": True, "task": updated}

    payload = {
        "text": task.get("copy_text", ""),
        "profile_ids": [task.get("profile_id", "")],
        "media": {"link": task.get("video_url", "")},
    }
    if task.get("scheduled_at"):
        payload["scheduled_at"] = task.get("scheduled_at")

    try:
        result = publish_to_buffer(
            get_proxy_url_for_account(db_path, task.get("account_id", "")),
            account.get("buffer_token", ""),
            payload,
        )
        updates = {
            "status": "success" if result.get("success", True) else "failed",
            "buffer_response": result,
            "buffer_update_id": result.get("update_id", ""),
            "tiktok_url": result.get("tiktok_url", ""),
            "finished_at": content_now_iso(),
        }
        if updates["status"] == "failed":
            updates["error"] = result.get("error", "Buffer request failed")
    except requests.exceptions.RequestException as error:
        updates = {
            "status": "failed",
            "error": str(error),
            "finished_at": content_now_iso(),
        }

    return {"processed": True, "task": update_publish_task(data_dir, task["id"], updates)}


def build_tiktok_sampler_command(task):
    selector = str(
        task.get("ads_power_profile_id")
        or task.get("account_id")
        or task.get("profile_id")
        or ""
    ).strip()
    if not selector:
        raise ValueError("profile_id is required for sampling")
    url = str(task.get("tiktok_url") or "").strip()
    if not url:
        raise ValueError("tiktok_url is required for sampling")
    return [
        "npm",
        "run",
        "tiktok-sampler",
        "--",
        "--profile-id",
        selector,
        "--url",
        url,
    ]


def buffer_post_id_for_task(task):
    if task.get("buffer_update_id"):
        return str(task.get("buffer_update_id"))
    response = task.get("buffer_response") or {}
    if isinstance(response, dict):
        if response.get("update_id"):
            return str(response.get("update_id"))
        update_ids = response.get("update_ids") or []
        if update_ids:
            return str(update_ids[0])
    return ""


def execute_next_tiktok_link_backfill(data_dir, db_path):
    task = next_pending_tiktok_link_backfill(data_dir)
    if task is None:
        return {"processed": False, "task": None}

    account = get_buffer_account(db_path, task.get("account_id", ""))
    if account is None:
        return {
            "processed": True,
            "task": mark_tiktok_link_backfill_failure(
                data_dir,
                task["id"],
                "account not found",
            ),
        }

    try:
        buffer_payload = fetch_buffer_post(
            account.get("buffer_token", ""),
            buffer_post_id_for_task(task),
            get_proxy_url_for_account(db_path, task.get("account_id", "")),
        )
        tiktok_url = extract_tiktok_url_from_buffer_payload(buffer_payload)
        if not tiktok_url:
            return {
                "processed": True,
                "task": mark_tiktok_link_backfill_failure(
                    data_dir,
                    task["id"],
                    "Buffer post does not include a TikTok URL yet",
                ),
            }
        return {
            "processed": True,
            "task": mark_tiktok_link_backfill_success(data_dir, task["id"], tiktok_url),
        }
    except requests.exceptions.RequestException as error:
        return {
            "processed": True,
            "task": mark_tiktok_link_backfill_failure(data_dir, task["id"], str(error)),
        }


def execute_next_publish_sample(data_dir, min_age_hours=24):
    task = next_due_publish_sample(data_dir, min_age_hours=min_age_hours)
    if task is None:
        return {"processed": False, "task": None}

    try:
        command = build_tiktok_sampler_command(task)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            shell=False,
        )
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "sampler failed").strip()
            return {
                "processed": True,
                "task": mark_publish_sample_failure(data_dir, task["id"], error),
            }
        metrics = json.loads(completed.stdout or "{}")
        return {
            "processed": True,
            "task": mark_publish_sample_success(data_dir, task["id"], metrics),
        }
    except (json.JSONDecodeError, subprocess.SubprocessError, ValueError) as error:
        return {
            "processed": True,
            "task": mark_publish_sample_failure(data_dir, task["id"], str(error)),
        }


def enrich_publish_stats_with_accounts(stats, db_path):
    accounts = account_summary(db_path).get("accounts", [])
    account_names = {}
    profile_names = {}
    for account in accounts:
        display_name = account.get("account_name") or account.get("buffer_account_id") or account.get("id") or ""
        for key in (
            account.get("id"),
            account.get("ads_power_user_id"),
            account.get("buffer_account_id"),
        ):
            if key:
                account_names[str(key)] = display_name
        for profile_id in account.get("buffer_profile_ids") or []:
            profile_names[str(profile_id)] = display_name
        for channel in account.get("buffer_channels") or []:
            channel_id = channel.get("id")
            channel_name = (
                channel.get("descriptor")
                or channel.get("name")
                or channel.get("serviceUsername")
                or display_name
            )
            if channel_id:
                profile_names[str(channel_id)] = str(channel_name)

    for item in stats.get("items", []):
        account_name = account_names.get(str(item.get("account_id") or ""), item.get("account_id", ""))
        item["account_name"] = account_name
        item["tiktok_account_name"] = (
            tiktok_username_from_url(item.get("tiktok_url", ""))
            or profile_names.get(str(item.get("profile_id") or ""), "")
            or account_name
        )
    return stats


def tiktok_username_from_url(url):
    match = re.search(r"(?:^|/)@([^/?#]+)", str(url or ""))
    if not match:
        return ""
    username = match.group(1).strip()
    return f"@{username}" if username else ""


def publish_sampling_options():
    settings = load_settings()
    sampling = settings.get("publish_sampling", {})
    interval = sampling.get("interval_seconds", 300)
    min_age_hours = sampling.get("min_age_hours", 24)
    try:
        interval = max(int(interval or 300), 30)
    except (TypeError, ValueError):
        interval = 300
    try:
        min_age_hours = max(int(24 if min_age_hours is None else min_age_hours), 0)
    except (TypeError, ValueError):
        min_age_hours = 24
    return {
        "enabled": sampling.get("enabled", True) is not False,
        "interval_seconds": interval,
        "min_age_hours": min_age_hours,
    }


def execute_publish_sampling_tick(data_dir, db_path):
    options = publish_sampling_options()
    if not options["enabled"]:
        return {"enabled": False, "backfill": None, "sample": None, "options": options}
    return {
        "enabled": True,
        "options": options,
        "backfill": execute_next_tiktok_link_backfill(data_dir, db_path),
        "sample": execute_next_publish_sample(
            data_dir,
            min_age_hours=options["min_age_hours"],
        ),
    }


_publish_worker_started = False
_publish_sampling_worker_started = False




def start_publish_queue_worker(app):
    global _publish_worker_started
    if _publish_worker_started:
        return
    _publish_worker_started = True

    def worker_loop():
        while True:
            settings = load_settings()
            interval = settings.get("publish_queue", {}).get("interval_seconds", 8)
            try:
                interval = max(int(interval or 8), 1)
            except (TypeError, ValueError):
                interval = 8

            try:
                execute_next_publish_task(
                    app.config["CONTENT_DATA_DIR"],
                    app.config["ACCOUNTS_DB_PATH"],
                )
                execute_next_tiktok_link_backfill(
                    app.config["CONTENT_DATA_DIR"],
                    app.config["ACCOUNTS_DB_PATH"],
                )
                execute_next_publish_sample(app.config["CONTENT_DATA_DIR"])
            except Exception:
                pass

            time.sleep(interval)

    thread = threading.Thread(target=worker_loop, name="publish-queue-worker", daemon=True)
    thread.start()


def start_publish_sampling_worker(app):
    global _publish_sampling_worker_started
    if _publish_sampling_worker_started:
        return
    _publish_sampling_worker_started = True

    def worker_loop():
        while True:
            options = publish_sampling_options()
            try:
                execute_publish_sampling_tick(
                    app.config["CONTENT_DATA_DIR"],
                    app.config["ACCOUNTS_DB_PATH"],
                )
            except Exception:
                pass

            time.sleep(options["interval_seconds"])

    thread = threading.Thread(target=worker_loop, name="publish-sampling-worker", daemon=True)
    thread.start()


def select_publish_accounts(accounts, account_ids):
    if not account_ids:
        return accounts

    by_id = {}
    for account in accounts:
        for key in (
            account.get("id"),
            account.get("ads_power_user_id"),
            account.get("buffer_account_id"),
        ):
            if key:
                by_id[str(key)] = account

    selected = []
    for account_id in account_ids:
        account = by_id.get(str(account_id))
        if account and account not in selected:
            selected.append(account)
    return selected
