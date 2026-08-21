"""Content and publishing routes (migrated from gateway/app.py)."""

from __future__ import annotations

import requests
from flask import Blueprint, current_app, jsonify, request

from gateway.account_store import account_summary
from gateway.content_import import parse_copy_import
from gateway.content_store import (
    add_copy_item,
    apply_copy_import,
    create_brand,
    delete_batch_publish_run,
    delete_daily_schedule,
    list_batch_publish_runs,
    list_brands,
    list_copy_items,
    list_daily_schedules,
    public_publish_tasks,
    publish_stats,
    rename_brand,
    save_batch_publish_run,
    save_daily_schedule,
    sync_video_library,
    update_batch_publish_run,
    update_daily_schedule,
    update_publish_metrics,
    unused_videos,
    video_summary,
)
from gateway.publish_queue import (
    create_publish_task,
    enrich_publish_stats_with_accounts,
    execute_next_publish_sample,
    execute_next_publish_task,
    execute_next_tiktok_link_backfill,
    execute_publish_sampling_tick,
    select_publish_accounts,
)
from gateway.r2_client import list_r2_video_objects
from gateway.settings_store import load_settings


def create_routes_publish_blueprint() -> Blueprint:
    bp = Blueprint("publish", __name__)

    @bp.post("/api/content/videos/sync")
    def sync_content_videos_route():
        try:
            videos = list_r2_video_objects(load_settings())
            return jsonify(
                sync_video_library(current_app.config["CONTENT_DATA_DIR"], videos)
            )
        except (ValueError, requests.exceptions.RequestException) as error:
            return jsonify({"error": str(error)}), 400

    @bp.get("/api/content/videos")
    def content_videos_route():
        return jsonify(video_summary(current_app.config["CONTENT_DATA_DIR"]))

    @bp.get("/api/content/brands")
    def content_brands_route():
        return jsonify(
            {"brands": list_brands(current_app.config["CONTENT_DATA_DIR"])}
        )

    @bp.post("/api/content/brands")
    def create_content_brand_route():
        payload = request.get_json(silent=True) or {}
        try:
            brand = create_brand(
                current_app.config["CONTENT_DATA_DIR"],
                payload.get("brand", ""),
            )
            return jsonify(
                {
                    "brand": brand,
                    "brands": list_brands(current_app.config["CONTENT_DATA_DIR"]),
                }
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    @bp.patch("/api/content/brands/<brand_id>")
    def rename_content_brand_route(brand_id):
        payload = request.get_json(silent=True) or {}
        try:
            brand = rename_brand(
                current_app.config["CONTENT_DATA_DIR"],
                brand_id,
                payload.get("name", ""),
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        if brand is None:
            return jsonify({"error": "品牌不存在"}), 404
        return jsonify(
            {
                "brand": brand,
                "brands": list_brands(current_app.config["CONTENT_DATA_DIR"]),
            }
        )

    @bp.post("/api/content/copy")
    def add_content_copy_route():
        payload = request.get_json(silent=True) or {}
        try:
            item = add_copy_item(
                current_app.config["CONTENT_DATA_DIR"],
                payload.get("brand_id", ""),
                payload.get("body", ""),
                payload.get("tags", []),
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        items = list_copy_items(
            current_app.config["CONTENT_DATA_DIR"],
            payload.get("brand_id", ""),
        )
        return jsonify(
            {"item": item, "copy_count": len(items), "items": items}
        )

    @bp.post("/api/content/copy/import")
    def import_content_copy_route():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "请选择导入文件"}), 400

        try:
            parsed = parse_copy_import(upload.filename, upload.stream)
            result = apply_copy_import(
                current_app.config["CONTENT_DATA_DIR"],
                parsed,
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        return jsonify(
            {
                **result,
                "brands": list_brands(
                    current_app.config["CONTENT_DATA_DIR"]
                ),
            }
        )

    @bp.get("/api/content/brands/<brand_id>/copy")
    def content_copy_route(brand_id):
        items = list_copy_items(
            current_app.config["CONTENT_DATA_DIR"],
            brand_id,
        )
        return jsonify(
            {"brand_id": brand_id, "copy_count": len(items), "items": items}
        )

    @bp.post("/api/publish/queue/manual-test")
    def manual_publish_test_route():
        payload = request.get_json(silent=True) or {}
        try:
            task = create_publish_task(
                data_dir=current_app.config["CONTENT_DATA_DIR"],
                db_path=current_app.config["ACCOUNTS_DB_PATH"],
                account_id=payload.get("account_id", ""),
                profile_id=payload.get("profile_id", ""),
                video_id=payload.get("video_id", ""),
                brand_id=payload.get("brand_id", ""),
                copy_id=payload.get("copy_id", ""),
                scheduled_at=payload.get("scheduled_at", ""),
            )
            if task.get("status") == "failed":
                return (
                    jsonify(
                        {"task": task, "error": task.get("error", "")}
                    ),
                    502,
                )
            return jsonify({"task": task})
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    @bp.post("/api/publish/queue/batch")
    def batch_publish_queue_route():
        payload = request.get_json(silent=True) or {}
        brand_id = payload.get("brand_id", "")
        scheduled_at = payload.get("scheduled_at", "")
        account_ids = payload.get("account_ids") or []
        accounts = select_publish_accounts(
            account_summary(
                current_app.config["ACCOUNTS_DB_PATH"]
            ).get("available_accounts", []),
            account_ids,
        )
        videos = unused_videos(current_app.config["CONTENT_DATA_DIR"])

        tasks = []
        requested = len(accounts)
        for account, video in zip(accounts, videos):
            profile_ids = account.get("buffer_profile_ids") or []
            if not profile_ids:
                continue
            try:
                tasks.append(
                    create_publish_task(
                        data_dir=current_app.config["CONTENT_DATA_DIR"],
                        db_path=current_app.config["ACCOUNTS_DB_PATH"],
                        account_id=(
                            account.get("ads_power_user_id")
                            or account.get("id")
                        ),
                        profile_id=profile_ids[0],
                        video_id=video.get("id", ""),
                        brand_id=brand_id,
                        scheduled_at=scheduled_at,
                        publish_now=False,
                    )
                )
            except ValueError as error:
                return jsonify({"error": str(error)}), 400

        skipped = max(requested - len(tasks), 0)
        response = {
            "requested": requested,
            "created": len(tasks),
            "skipped": skipped,
            "tasks": tasks,
        }
        if skipped:
            response["skipped_reason"] = "not enough unused videos"
        response["run"] = save_batch_publish_run(
            current_app.config["CONTENT_DATA_DIR"],
            {
                "scheduled_at": scheduled_at,
                "brand_id": brand_id,
                "account_ids": [
                    account.get("ads_power_user_id") or account.get("id")
                    for account in accounts
                ],
                "requested": requested,
                "created": len(tasks),
                "skipped": skipped,
                "status": "created",
            },
        )
        return jsonify(response)

    @bp.get("/api/publish/queue/batches")
    def batch_publish_runs_route():
        runs = list_batch_publish_runs(
            current_app.config["CONTENT_DATA_DIR"]
        )
        return jsonify({"count": len(runs), "runs": runs})

    @bp.post("/api/publish/queue/process-one")
    def process_one_publish_task_route():
        return jsonify(
            execute_next_publish_task(
                current_app.config["CONTENT_DATA_DIR"],
                current_app.config["ACCOUNTS_DB_PATH"],
            )
        )

    @bp.patch("/api/publish/queue/batches/<run_id>")
    def update_batch_publish_run_route(run_id):
        payload = request.get_json(silent=True) or {}
        run = update_batch_publish_run(
            current_app.config["CONTENT_DATA_DIR"],
            run_id,
            payload,
        )
        if run is None:
            return jsonify({"error": "batch run not found"}), 404
        return jsonify({"run": run})

    @bp.delete("/api/publish/queue/batches/<run_id>")
    def delete_batch_publish_run_route(run_id):
        deleted = delete_batch_publish_run(
            current_app.config["CONTENT_DATA_DIR"],
            run_id,
        )
        if not deleted:
            return jsonify({"error": "batch run not found"}), 404
        return jsonify({"deleted": True})

    @bp.get("/api/publish/results")
    def publish_results_route():
        tasks = public_publish_tasks(
            current_app.config["CONTENT_DATA_DIR"],
            date=request.args.get("date"),
            status=None,
        )
        summary = {
            "task_count": len(tasks),
            "success": sum(item.get("status") == "success" for item in tasks),
            "failed": sum(item.get("status") == "failed" for item in tasks),
            "pending": sum(item.get("status") == "pending" for item in tasks),
        }
        status = request.args.get("status", "").strip()
        if status:
            tasks = [item for item in tasks if item.get("status") == status]
        query = request.args.get("query", "").strip().casefold()
        if query:
            fields = ("id", "account_id", "account_name", "profile_id", "copy_text", "error", "tiktok_url")
            tasks = [
                item for item in tasks
                if any(query in str(item.get(field) or "").casefold() for field in fields)
            ]
        if not any(key in request.args for key in ("page", "page_size", "query")):
            return jsonify({"count": len(tasks), "tasks": tasks})
        try:
            page = max(int(request.args.get("page", 1)), 1)
            page_size = min(max(int(request.args.get("page_size", 50)), 1), 200)
        except (TypeError, ValueError):
            return jsonify({"error": "page and page_size must be positive integers"}), 400
        total = len(tasks)
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        return jsonify({
            "count": len(tasks[start:start + page_size]),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "summary": summary,
            "tasks": tasks[start:start + page_size],
        })

    @bp.get("/api/publish/stats")
    def publish_stats_route():
        stats = publish_stats(
            current_app.config["CONTENT_DATA_DIR"],
            date=request.args.get("date"),
            status=request.args.get("status"),
            sort=request.args.get("sort"),
        )
        return jsonify(
            enrich_publish_stats_with_accounts(
                stats,
                current_app.config["ACCOUNTS_DB_PATH"],
            )
        )

    @bp.post("/api/publish/auto-sample-tick")
    def publish_auto_sample_tick_route():
        return jsonify(
            execute_publish_sampling_tick(
                current_app.config["CONTENT_DATA_DIR"],
                current_app.config["ACCOUNTS_DB_PATH"],
            )
        )

    @bp.post("/api/publish/sample-next")
    def publish_sample_next_route():
        try:
            min_age_hours = int(request.args.get("min_age_hours", 24))
        except (TypeError, ValueError):
            min_age_hours = 24
        return jsonify(
            execute_next_publish_sample(
                current_app.config["CONTENT_DATA_DIR"],
                min_age_hours=max(min_age_hours, 0),
            )
        )

    @bp.post("/api/publish/backfill-link-next")
    def publish_backfill_link_next_route():
        return jsonify(
            execute_next_tiktok_link_backfill(
                current_app.config["CONTENT_DATA_DIR"],
                current_app.config["ACCOUNTS_DB_PATH"],
            )
        )

    @bp.post("/api/publish/logs/cleanup")
    def cleanup_publish_logs_route():
        payload = request.get_json(silent=True) or {}
        return jsonify(
            cleanup_publish_logs(
                current_app.config["CONTENT_DATA_DIR"],
                payload.get("before_date", ""),
            )
        )

    @bp.post("/api/publish/schedule/daily")
    def daily_publish_schedule_route():
        payload = request.get_json(silent=True) or {}
        return jsonify(
            {
                "schedule": save_daily_schedule(
                    current_app.config["CONTENT_DATA_DIR"],
                    payload,
                )
            }
        )

    @bp.get("/api/publish/schedule/daily")
    def daily_publish_schedules_route():
        schedules = list_daily_schedules(
            current_app.config["CONTENT_DATA_DIR"]
        )
        return jsonify(
            {"count": len(schedules), "schedules": schedules}
        )

    @bp.patch("/api/publish/schedule/daily/<schedule_id>")
    def update_daily_publish_schedule_route(schedule_id):
        payload = request.get_json(silent=True) or {}
        schedule = update_daily_schedule(
            current_app.config["CONTENT_DATA_DIR"],
            schedule_id,
            payload,
        )
        if schedule is None:
            return jsonify({"error": "schedule not found"}), 404
        return jsonify({"schedule": schedule})

    @bp.delete("/api/publish/schedule/daily/<schedule_id>")
    def delete_daily_publish_schedule_route(schedule_id):
        deleted = delete_daily_schedule(
            current_app.config["CONTENT_DATA_DIR"],
            schedule_id,
        )
        if not deleted:
            return jsonify({"error": "schedule not found"}), 404
        return jsonify({"deleted": True})

    @bp.post("/api/publish/results/metrics")
    def publish_metrics_route():
        payload = request.get_json(silent=True) or {}
        task = update_publish_metrics(
            current_app.config["CONTENT_DATA_DIR"],
            payload.get("task_id", ""),
            payload.get("views", 0),
            payload.get("comments", 0),
            payload.get("tiktok_url", ""),
        )
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify({"task": task})

    return bp
