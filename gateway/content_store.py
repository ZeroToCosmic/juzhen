import hashlib
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_CONTENT_DIR = Path("data") / "content"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def sync_video_library(data_dir, video_objects):
    data_dir = Path(data_dir)
    path = data_dir / "videos.json"
    current = read_json(path, {"videos": []})
    existing_by_key = {item.get("key"): item for item in current.get("videos", [])}
    existing_by_name = {
        Path(str(item.get("key") or item.get("url") or "")).name: item
        for item in current.get("videos", [])
    }
    videos = []

    for index, item in enumerate(video_objects, start=1):
        key = item.get("key") or item.get("url") or f"video-{index}"
        existing = existing_by_key.get(key) or existing_by_name.get(Path(str(key)).name) or {}
        videos.append(
            {
                "id": existing.get("id") or f"video-{index}",
                "key": key,
                "url": item.get("url", ""),
                "used": bool(existing.get("used", False)),
                "synced_at": now_iso(),
            }
        )

    write_json(path, {"videos": videos})
    return video_summary(data_dir)


def video_summary(data_dir):
    videos = _videos(data_dir)
    return {
        "video_count": len(videos),
        "available_count": len([item for item in videos if not item.get("used")]),
        "used_count": len([item for item in videos if item.get("used")]),
        "videos": [_public_video(item) for item in videos],
    }


def get_video(data_dir, video_id):
    return next((item for item in _videos(data_dir) if item.get("id") == video_id), None)


def mark_video_used(data_dir, video_id):
    path = Path(data_dir) / "videos.json"
    payload = read_json(path, {"videos": []})
    for item in payload.get("videos", []):
        if item.get("id") == video_id:
            item["used"] = True
            item["used_at"] = now_iso()
            write_json(path, payload)
            return item
    return None


def unused_videos(data_dir):
    return [item for item in _videos(data_dir) if not item.get("used")]


def create_brand(data_dir, brand_name):
    brand_name = normalize_brand_name(brand_name)
    if not brand_name:
        raise ValueError("brand is required")

    existing = find_brand_by_name(data_dir, brand_name)
    if existing is not None:
        return existing

    brand_id = brand_id_for_name(data_dir, brand_name)
    brand_dir = Path(data_dir) / "brands" / brand_id
    brand_dir.mkdir(parents=True, exist_ok=True)
    meta = {"id": brand_id, "name": brand_name, "updated_at": now_iso()}
    write_json(brand_dir / "brand.json", meta)
    if not (brand_dir / "copy.json").exists():
        write_json(brand_dir / "copy.json", {"items": []})
    return meta


def list_brands(data_dir):
    brands_dir = Path(data_dir) / "brands"
    if not brands_dir.exists():
        return []

    brands = []
    for brand_dir in sorted(brands_dir.iterdir()):
        if brand_dir.is_dir():
            brand = read_json(
                brand_dir / "brand.json",
                {"id": brand_dir.name, "name": brand_dir.name},
            )
            copy_payload = read_json(brand_dir / "copy.json", {"items": []})
            brand["copy_count"] = len(copy_payload.get("items", []))
            brand["updated_at"] = brand.get("updated_at", "")
            brands.append(brand)
    return brands


def normalize_brand_name(value):
    return str(value or "").strip()


def find_brand_by_name(data_dir, brand_name):
    target = normalize_brand_name(brand_name).casefold()
    return next(
        (
            brand
            for brand in list_brands(data_dir)
            if str(brand.get("name") or "").casefold() == target
        ),
        None,
    )


def brand_id_for_name(data_dir, brand_name):
    ascii_slug = slugify(brand_name)
    candidate = ascii_slug if ascii_slug != "brand" else ""
    if candidate and not (Path(data_dir) / "brands" / candidate).exists():
        return candidate

    digest = hashlib.sha256(
        normalize_brand_name(brand_name).casefold().encode("utf-8")
    ).hexdigest()
    for length in range(10, len(digest) + 1, 2):
        candidate = f"brand-{digest[:length]}"
        if not (Path(data_dir) / "brands" / candidate).exists():
            return candidate
    raise ValueError("brand id collision")


def rename_brand(data_dir, brand_id, new_name):
    new_name = normalize_brand_name(new_name)
    if not new_name:
        raise ValueError("品牌名称不能为空")

    path = Path(data_dir) / "brands" / brand_id / "brand.json"
    if not path.exists():
        return None

    duplicate = find_brand_by_name(data_dir, new_name)
    if duplicate and duplicate.get("id") != brand_id:
        raise ValueError("品牌名称已存在")

    brand = read_json(path, {"id": brand_id})
    brand.update({"id": brand_id, "name": new_name, "updated_at": now_iso()})
    write_json(path, brand)
    return brand


def _touch_brand(data_dir, brand_id):
    path = Path(data_dir) / "brands" / brand_id / "brand.json"
    if not path.exists():
        return
    brand = read_json(path, {"id": brand_id, "name": brand_id})
    brand["updated_at"] = now_iso()
    write_json(path, brand)


def add_copy_item(data_dir, brand_id, body, tags):
    body = (body or "").strip()
    if not brand_id or not body:
        raise ValueError("brand_id and body are required")

    path = Path(data_dir) / "brands" / brand_id / "copy.json"
    payload = read_json(path, {"items": []})
    items = payload.get("items", [])
    item = {
        "id": f"copy-{len(items) + 1}",
        "body": body,
        "tags": parse_tags(tags),
        "created_at": now_iso(),
    }
    items.append(item)
    write_json(path, {"items": items})
    _touch_brand(data_dir, brand_id)
    return item


def copy_fingerprint(body, tags):
    return str(body or "").strip(), tuple(parse_tags(tags))


def apply_copy_import(data_dir, parsed):
    result = {
        "total": parsed["total"],
        "created": 0,
        "duplicates": 0,
        "failed": len(parsed["errors"]),
        "brands_created": 0,
        "brand_results": [],
        "errors": list(parsed["errors"]),
    }
    grouped = {}
    for row in parsed["rows"]:
        grouped.setdefault(row["brand_name"].casefold(), []).append(row)

    for rows in grouped.values():
        brand = find_brand_by_name(data_dir, rows[0]["brand_name"])
        if brand is None:
            brand = create_brand(data_dir, rows[0]["brand_name"])
            result["brands_created"] += 1

        path = Path(data_dir) / "brands" / brand["id"] / "copy.json"
        payload = read_json(path, {"items": []})
        items = payload.get("items", [])
        seen = {
            copy_fingerprint(item.get("body", ""), item.get("tags", []))
            for item in items
        }
        brand_created = 0
        brand_duplicates = 0

        for row in rows:
            fingerprint = copy_fingerprint(row["body"], row["tags"])
            if fingerprint in seen:
                brand_duplicates += 1
                result["duplicates"] += 1
                continue

            seen.add(fingerprint)
            items.append(
                {
                    "id": f"copy-{len(items) + 1}",
                    "body": row["body"],
                    "tags": parse_tags(row["tags"]),
                    "created_at": now_iso(),
                }
            )
            brand_created += 1
            result["created"] += 1

        write_json(path, {"items": items})
        _touch_brand(data_dir, brand["id"])
        result["brand_results"].append(
            {
                "brand_id": brand["id"],
                "brand_name": brand["name"],
                "created": brand_created,
                "duplicates": brand_duplicates,
            }
        )

    return result


def list_copy_items(data_dir, brand_id):
    payload = read_json(Path(data_dir) / "brands" / brand_id / "copy.json", {"items": []})
    return payload.get("items", [])


def get_copy_item(data_dir, brand_id, copy_id=None):
    items = list_copy_items(data_dir, brand_id)
    if not items:
        return None
    if not copy_id:
        return random.choice(items)
    return next((item for item in items if item.get("id") == copy_id), None)


def compose_text(copy_item):
    tags = copy_item.get("tags") or []
    tag_text = " ".join(tags)
    return f"{copy_item.get('body', '')}\n\n{tag_text}".strip()


def save_publish_task(data_dir, task):
    path = Path(data_dir) / "publish_tasks.json"
    payload = read_json(path, {"tasks": []})
    tasks = payload.get("tasks", [])
    if not task.get("id"):
        task["id"] = f"task-{len(tasks) + 1}"
    task.setdefault("created_at", now_iso())
    tasks.append(task)
    write_json(path, {"tasks": tasks})
    return task


def next_pending_publish_task(data_dir):
    tasks = read_json(Path(data_dir) / "publish_tasks.json", {"tasks": []}).get("tasks", [])
    return next((task for task in tasks if task.get("status") == "pending"), None)


def next_due_publish_sample(data_dir, now=None, min_age_hours=24):
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=min_age_hours)
    tasks = read_json(Path(data_dir) / "publish_tasks.json", {"tasks": []}).get("tasks", [])
    for task in tasks:
        if task.get("status") != "success":
            continue
        if not task.get("tiktok_url"):
            continue
        if task.get("sample_status") == "success":
            continue
        reference_time = _task_sample_reference_time(task)
        if reference_time is None or reference_time <= cutoff:
            return task
    return None


def next_pending_tiktok_link_backfill(data_dir, now=None):
    now = now or datetime.now().astimezone()
    tasks = read_json(Path(data_dir) / "publish_tasks.json", {"tasks": []}).get("tasks", [])
    for task in tasks:
        if task.get("status") != "success":
            continue
        if task.get("tiktok_url"):
            continue
        if not _buffer_post_id(task):
            continue
        if task.get("link_backfill_status") == "success":
            continue
        next_attempt_at = _parse_iso_datetime(task.get("link_backfill_next_attempt_at"))
        if next_attempt_at is not None and next_attempt_at > now:
            continue
        return task
    return None


def update_publish_task(data_dir, task_id, updates):
    path = Path(data_dir) / "publish_tasks.json"
    payload = read_json(path, {"tasks": []})
    for task in payload.get("tasks", []):
        if task.get("id") == task_id:
            task.update(updates)
            write_json(path, payload)
            return task
    return None


def save_batch_publish_run(data_dir, run):
    path = Path(data_dir) / "publish_batch_runs.json"
    payload = read_json(path, {"runs": []})
    runs = payload.get("runs", [])
    saved = {
        "id": run.get("id") or f"batch-{len(runs) + 1}",
        "created_at": now_iso(),
        "scheduled_at": str(run.get("scheduled_at") or "").strip(),
        "brand_id": str(run.get("brand_id") or "").strip(),
        "account_ids": [str(item) for item in run.get("account_ids", []) if item],
        "requested": int(run.get("requested") or 0),
        "created": int(run.get("created") or 0),
        "skipped": int(run.get("skipped") or 0),
        "status": str(run.get("status") or "created"),
    }
    runs.insert(0, saved)
    write_json(path, {"runs": runs})
    return saved


def list_batch_publish_runs(data_dir):
    return read_json(Path(data_dir) / "publish_batch_runs.json", {"runs": []}).get("runs", [])


def update_batch_publish_run(data_dir, run_id, updates):
    path = Path(data_dir) / "publish_batch_runs.json"
    payload = read_json(path, {"runs": []})
    for run in payload.get("runs", []):
        if run.get("id") == run_id:
            if "scheduled_at" in updates:
                run["scheduled_at"] = str(updates.get("scheduled_at") or "").strip()
            if "brand_id" in updates:
                run["brand_id"] = str(updates.get("brand_id") or "").strip()
            if "account_ids" in updates:
                run["account_ids"] = [
                    str(item) for item in updates.get("account_ids", []) if item
                ]
                run["requested"] = len(run["account_ids"])
            run["updated_at"] = now_iso()
            write_json(path, payload)
            return run
    return None


def delete_batch_publish_run(data_dir, run_id):
    path = Path(data_dir) / "publish_batch_runs.json"
    payload = read_json(path, {"runs": []})
    runs = payload.get("runs", [])
    kept = [run for run in runs if run.get("id") != run_id]
    write_json(path, {"runs": kept})
    return len(kept) != len(runs)


def list_publish_tasks(data_dir, date=None, status=None):
    tasks = read_json(Path(data_dir) / "publish_tasks.json", {"tasks": []}).get("tasks", [])
    if date:
        tasks = [task for task in tasks if str(task.get("created_at", "")).startswith(date)]
    if status:
        tasks = [task for task in tasks if task.get("status") == status]
    return tasks


def public_publish_tasks(data_dir, date=None, status=None):
    return [_public_publish_task(task) for task in list_publish_tasks(data_dir, date, status)]


def publish_stats(data_dir, date=None, status=None, sort=None):
    tasks = [
        task
        for task in list_publish_tasks(data_dir, date=date, status=status)
        if task.get("status") == "success"
    ]
    items = [_stats_item(task) for task in tasks]
    sort_fields = {
        "views": "views_24h",
        "views_24h": "views_24h",
        "likes": "likes_24h",
        "likes_24h": "likes_24h",
        "comments": "comments",
    }
    sort_field = sort_fields.get(str(sort or "views_24h"))
    if sort_field:
        items.sort(key=lambda item: item.get(sort_field, 0), reverse=True)
    return {
        "count": len(tasks),
        "success": len(tasks),
        "failed": 0,
        "pending": 0,
        "views": sum(item["views_24h"] for item in items),
        "views_24h": sum(item["views_24h"] for item in items),
        "likes_24h": sum(item["likes_24h"] for item in items),
        "comments": sum(item["comments"] for item in items),
        "engagement_count": sum(item["engagement_count"] for item in items),
        "items": items,
    }


def cleanup_publish_logs(data_dir, before_date):
    path = Path(data_dir) / "publish_tasks.json"
    payload = read_json(path, {"tasks": []})
    tasks = payload.get("tasks", [])
    kept = [
        task
        for task in tasks
        if not before_date or str(task.get("created_at", ""))[:10] >= before_date
    ]
    write_json(path, {"tasks": kept})
    return {"deleted": len(tasks) - len(kept)}


def save_daily_schedule(data_dir, schedule):
    path = Path(data_dir) / "publish_schedules.json"
    current = read_json(path, {"schedules": []})
    schedules = current.get("schedules", [])
    payload = {
        "id": schedule.get("id") or f"schedule-{len(schedules) + 1}",
        "enabled": bool(schedule.get("enabled")),
        "start_date": str(schedule.get("start_date") or "").strip(),
        "time": str(schedule.get("time") or "").strip(),
        "brand_id": str(schedule.get("brand_id") or "").strip(),
        "account_ids": [str(item) for item in schedule.get("account_ids", []) if item],
        "updated_at": now_iso(),
    }
    payload["account_count"] = len(payload["account_ids"])
    schedules.insert(0, payload)
    write_json(path, {"schedules": schedules})
    write_json(Path(data_dir) / "publish_schedule.json", payload)
    return payload


def list_daily_schedules(data_dir):
    return read_json(Path(data_dir) / "publish_schedules.json", {"schedules": []}).get("schedules", [])


def update_daily_schedule(data_dir, schedule_id, updates):
    path = Path(data_dir) / "publish_schedules.json"
    payload = read_json(path, {"schedules": []})
    for schedule in payload.get("schedules", []):
        if schedule.get("id") == schedule_id:
            if "enabled" in updates:
                schedule["enabled"] = bool(updates.get("enabled"))
            if "start_date" in updates:
                schedule["start_date"] = str(updates.get("start_date") or "").strip()
            if "time" in updates:
                schedule["time"] = str(updates.get("time") or "").strip()
            if "brand_id" in updates:
                schedule["brand_id"] = str(updates.get("brand_id") or "").strip()
            if "account_ids" in updates:
                schedule["account_ids"] = [
                    str(item) for item in updates.get("account_ids", []) if item
                ]
                schedule["account_count"] = len(schedule["account_ids"])
            schedule["updated_at"] = now_iso()
            write_json(path, payload)
            return schedule
    return None


def delete_daily_schedule(data_dir, schedule_id):
    path = Path(data_dir) / "publish_schedules.json"
    payload = read_json(path, {"schedules": []})
    schedules = payload.get("schedules", [])
    kept = [schedule for schedule in schedules if schedule.get("id") != schedule_id]
    write_json(path, {"schedules": kept})
    return len(kept) != len(schedules)


def update_publish_metrics(data_dir, task_id, views=0, comments=0, tiktok_url=""):
    path = Path(data_dir) / "publish_tasks.json"
    payload = read_json(path, {"tasks": []})
    for task in payload.get("tasks", []):
        if task.get("id") == task_id:
            task["views"] = int(views or 0)
            task["comments"] = int(comments or 0)
            if tiktok_url:
                task["tiktok_url"] = tiktok_url
            write_json(path, payload)
            return task
    return None


def mark_publish_sample_success(data_dir, task_id, metrics):
    updates = {
        "views_24h": _int_metric(metrics.get("views_24h", metrics.get("views", 0))),
        "likes_24h": _int_metric(metrics.get("likes_24h", metrics.get("likes", 0))),
        "comments": _int_metric(metrics.get("comments", 0)),
        "sample_status": "success",
        "sampled_at": now_iso(),
        "sample_error": "",
    }
    if metrics.get("country"):
        updates["country"] = str(metrics.get("country") or "").strip()
    return update_publish_task(data_dir, task_id, updates)


def mark_publish_sample_failure(data_dir, task_id, error):
    return update_publish_task(
        data_dir,
        task_id,
        {
            "sample_status": "failed",
            "sampled_at": now_iso(),
            "sample_error": str(error or "sample failed"),
        },
    )


def mark_tiktok_link_backfill_success(data_dir, task_id, tiktok_url):
    return update_publish_task(
        data_dir,
        task_id,
        {
            "tiktok_url": str(tiktok_url or "").strip(),
            "link_backfill_status": "success",
            "link_backfilled_at": now_iso(),
            "link_backfill_error": "",
        },
    )


def mark_tiktok_link_backfill_failure(data_dir, task_id, error):
    retry_minutes = 30 if "429" in str(error or "") or "Too Many Requests" in str(error or "") else 5
    next_attempt_at = datetime.now().astimezone() + timedelta(minutes=retry_minutes)
    return update_publish_task(
        data_dir,
        task_id,
        {
            "link_backfill_status": "failed",
            "link_backfilled_at": now_iso(),
            "link_backfill_error": str(error or "link backfill failed"),
            "link_backfill_next_attempt_at": next_attempt_at.isoformat(timespec="seconds"),
        },
    )


def _buffer_post_id(task):
    if task.get("buffer_update_id"):
        return task.get("buffer_update_id")
    response = task.get("buffer_response") or {}
    if isinstance(response, dict):
        return response.get("update_id") or next(iter(response.get("update_ids", []) or []), "")
    return ""


def _task_sample_reference_time(task):
    for field in ("finished_at", "scheduled_at", "created_at"):
        parsed = _parse_iso_datetime(task.get(field))
        if parsed is not None:
            return parsed
    return None


def _parse_iso_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def proxy_display(proxy_session):
    parts = str(proxy_session or "").split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return str(proxy_session or "")


def _public_publish_task(task):
    public = dict(task)
    public["proxy_display"] = proxy_display(task.get("proxy_session", ""))
    public.pop("proxy_session", None)
    return public


def _stats_item(task):
    views = _int_metric(task.get("views_24h", task.get("views", 0)))
    likes = _int_metric(task.get("likes_24h", task.get("likes", 0)))
    comments = _int_metric(task.get("comments", 0))
    return {
        "id": task.get("id", ""),
        "account_id": task.get("account_id", ""),
        "profile_id": task.get("profile_id", ""),
        "scheduled_at": task.get("scheduled_at", ""),
        "tiktok_url": task.get("tiktok_url", ""),
        "tiktok_url_short": _short_tiktok_url(task.get("tiktok_url", "")),
        "country": task.get("country", ""),
        "views_24h": views,
        "likes_24h": likes,
        "comments": comments,
        "engagement_count": likes + comments,
    }


def _int_metric(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _short_tiktok_url(url):
    value = str(url or "").strip()
    for prefix in ("https://www.tiktok.com/", "https://tiktok.com/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def parse_tags(tags):
    if isinstance(tags, list):
        return [str(tag).strip() for tag in tags if str(tag).strip()]
    return [tag for tag in re.split(r"[\s,，]+", str(tags or "")) if tag]


def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "brand"


def _videos(data_dir):
    return read_json(Path(data_dir) / "videos.json", {"videos": []}).get("videos", [])


def _public_video(video):
    return {
        "id": video.get("id", ""),
        "key": video.get("key", ""),
        "used": bool(video.get("used")),
    }
