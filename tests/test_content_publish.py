from contextlib import closing
from io import BytesIO
import sqlite3

import pytest
import requests
from openpyxl import Workbook

from gateway.app import create_app
from gateway.content_store import (
    apply_copy_import,
    create_brand,
    list_brands,
    list_copy_items,
    mark_publish_sample_failure,
    mark_publish_sample_success,
    mark_tiktok_link_backfill_failure,
    mark_tiktok_link_backfill_success,
    next_pending_tiktok_link_backfill,
    next_due_publish_sample,
    read_json,
    rename_brand,
    sync_video_library,
    write_json,
)
from init_db import init_db


def make_client(tmp_path, monkeypatch):
    data_dir = tmp_path / "content-data"
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    app = create_app()
    app.config["CONTENT_DATA_DIR"] = data_dir
    app.config["ACCOUNTS_DB_PATH"] = tmp_path / "accounts.db"
    init_db(app.config["ACCOUNTS_DB_PATH"])
    return app.test_client(), data_dir, app.config["ACCOUNTS_DB_PATH"]


def insert_account(
    db_path,
    account_id,
    profile_ids=None,
    proxy_session="203.0.113.8:9000:user2:pass2",
):
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id,
                buffer_account_id,
                proxy_session,
                account_name,
                buffer_token,
                buffer_profile_ids,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                account_id,
                account_id,
                proxy_session,
                f"Account {account_id}",
                f"token-{account_id}",
                '["profile-a"]' if profile_ids is None else profile_ids,
            ),
        )


def api_xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def test_sync_r2_videos_saves_library_without_returning_video_links(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "gateway.routes_publish.list_r2_video_objects",
        lambda settings: [
            {"key": "videos/one.mp4", "url": "https://cdn.example.com/one.mp4"},
            {"key": "videos/two.mov", "url": "https://cdn.example.com/two.mov"},
        ],
    )

    response = client.post("/api/content/videos/sync")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["video_count"] == 2
    assert "https://cdn.example.com/one.mp4" not in response.get_data(as_text=True)

    list_response = client.get("/api/content/videos")
    assert list_response.status_code == 200
    assert list_response.get_json()["available_count"] == 2
    assert "https://cdn.example.com/two.mov" not in list_response.get_data(as_text=True)


def test_sync_video_library_preserves_used_state_when_folder_encoding_changes(tmp_path):
    data_dir = tmp_path / "content"
    write_json(
        data_dir / "videos.json",
        {
            "videos": [
                {
                    "id": "video-1",
                    "key": "wrong-folder/0710 (1).mp4",
                    "url": "https://media.ttvid.org/wrong-folder/0710%20(1).mp4",
                    "used": True,
                }
            ]
        },
    )

    sync_video_library(
        data_dir,
        [
            {
                "key": "背景/0710 (1).mp4",
                "url": "https://media.ttvid.org/%E8%83%8C%E6%99%AF/0710%20(1).mp4",
            }
        ],
    )

    payload = (data_dir / "videos.json").read_text(encoding="utf-8")
    assert '"id": "video-1"' in payload
    assert '"used": true' in payload
    assert "%E8%83%8C%E6%99%AF/0710%20(1).mp4" in payload


def test_brand_copy_manager_creates_brand_folder_and_copy_items(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)

    brand_response = client.post("/api/content/brands", json={"brand": "Brand One"})
    copy_response = client.post(
        "/api/content/copy",
        json={
            "brand_id": "brand-one",
            "body": "今天发布一条新品视频",
            "tags": "#new #tiktok",
        },
    )

    assert brand_response.status_code == 200
    assert brand_response.get_json()["brand"]["id"] == "brand-one"
    assert (data_dir / "brands" / "brand-one" / "copy.json").exists()
    assert copy_response.status_code == 200
    assert copy_response.get_json()["copy_count"] == 1

    list_response = client.get("/api/content/brands/brand-one/copy")
    assert list_response.get_json()["items"][0]["tags"] == ["#new", "#tiktok"]


def test_copy_import_groups_brands_and_skips_duplicate_rows(tmp_path):
    data_dir = tmp_path / "content"
    create_brand(data_dir, "Brand One")

    result = apply_copy_import(
        data_dir,
        {
            "total": 4,
            "errors": [{"row": 5, "error": "缺少文案"}],
            "rows": [
                {
                    "row": 2,
                    "brand_name": "Brand One",
                    "body": "正文一",
                    "tags": "#a #b",
                },
                {
                    "row": 3,
                    "brand_name": "品牌二",
                    "body": "正文二",
                    "tags": "#c",
                },
                {
                    "row": 4,
                    "brand_name": "brand one",
                    "body": "正文一",
                    "tags": "#a #b",
                },
            ],
        },
    )

    assert result["created"] == 2
    assert result["duplicates"] == 1
    assert result["failed"] == 1
    assert result["brands_created"] == 1
    brands = list_brands(data_dir)
    assert len(brands) == 2
    assert sorted(brand["copy_count"] for brand in brands) == [1, 1]
    chinese_brand = next(brand for brand in brands if brand["name"] == "品牌二")
    assert chinese_brand["id"].startswith("brand-")
    assert chinese_brand["updated_at"]


def test_copy_import_skips_copy_already_saved_for_brand(tmp_path):
    data_dir = tmp_path / "content"
    brand = create_brand(data_dir, "Brand One")
    first = {
        "total": 1,
        "errors": [],
        "rows": [
            {
                "row": 2,
                "brand_name": "Brand One",
                "body": "相同正文",
                "tags": "#a,#b",
            }
        ],
    }

    apply_copy_import(data_dir, first)
    second = apply_copy_import(data_dir, first)

    assert second["created"] == 0
    assert second["duplicates"] == 1
    assert len(list_copy_items(data_dir, brand["id"])) == 1


def test_chinese_brands_receive_distinct_stable_folder_ids(tmp_path):
    data_dir = tmp_path / "content"

    first = create_brand(data_dir, "品牌一")
    second = create_brand(data_dir, "品牌二")
    repeated = create_brand(data_dir, " 品牌一 ")

    assert first["id"].startswith("brand-")
    assert second["id"].startswith("brand-")
    assert first["id"] != second["id"]
    assert repeated["id"] == first["id"]


def test_rename_brand_preserves_id_and_rejects_duplicate_name(tmp_path):
    data_dir = tmp_path / "content"
    brand = create_brand(data_dir, "Brand One")
    create_brand(data_dir, "Brand Two")

    renamed = rename_brand(data_dir, brand["id"], "Brand One New")

    assert renamed["id"] == brand["id"]
    assert renamed["name"] == "Brand One New"
    assert (data_dir / "brands" / brand["id"] / "brand.json").exists()
    with pytest.raises(ValueError, match="已存在"):
        rename_brand(data_dir, brand["id"], "brand two")

    assert rename_brand(data_dir, "missing-brand", "Missing") is None


def test_copy_import_api_accepts_xlsx_and_returns_summary(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)
    source = api_xlsx_bytes(
        [
            ["品牌名", "文案", "tag"],
            ["品牌一", "正文一", "#a"],
            ["品牌二", "正文二", "#b"],
            ["品牌一", "正文一", "#a"],
        ]
    )

    response = client.post(
        "/api/content/copy/import",
        data={"file": (source, "copy.xlsx")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["created"] == 2
    assert payload["duplicates"] == 1
    assert payload["brands_created"] == 2
    assert len(payload["brands"]) == 2


def test_brand_rename_api_preserves_id_and_handles_errors(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)
    first = client.post(
        "/api/content/brands",
        json={"brand": "Brand One"},
    ).get_json()["brand"]
    client.post("/api/content/brands", json={"brand": "Brand Two"})

    response = client.patch(
        f"/api/content/brands/{first['id']}",
        json={"name": "Brand New"},
    )
    duplicate = client.patch(
        f"/api/content/brands/{first['id']}",
        json={"name": "brand two"},
    )
    missing = client.patch(
        "/api/content/brands/missing-brand",
        json={"name": "Missing"},
    )

    assert response.status_code == 200
    assert response.get_json()["brand"]["id"] == first["id"]
    assert response.get_json()["brand"]["name"] == "Brand New"
    assert duplicate.status_code == 400
    assert missing.status_code == 404


def test_copy_import_api_rejects_missing_or_oversized_file(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)

    missing = client.post("/api/content/copy/import")
    client.application.config["MAX_CONTENT_LENGTH"] = 64
    oversized = client.post(
        "/api/content/copy/import",
        data={"file": (BytesIO(b"x" * 512), "copy.csv")},
        content_type="multipart/form-data",
    )

    assert missing.status_code == 400
    assert missing.get_json()["error"] == "请选择导入文件"
    assert oversized.status_code == 413
    assert oversized.get_json()["error"] == "导入文件不能超过 10 MB"


def test_manual_publish_test_creates_buffer_update_and_result(
    monkeypatch,
    tmp_path,
):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(
        db_path,
        "buffer-account-1",
        profile_ids='["profile-a"]',
        proxy_session="203.0.113.8:9000:user2:pass2",
    )
    write_json(
        data_dir / "videos.json",
        {
            "videos": [
                {
                    "id": "video-1",
                    "key": "videos/one.mp4",
                    "url": "https://cdn.example.com/one.mp4",
                    "used": False,
                }
            ]
        },
    )
    client.post("/api/content/brands", json={"brand": "Brand One"})
    client.post(
        "/api/content/copy",
        json={"brand_id": "brand-one", "body": "正文", "tags": ["#tag"]},
    )
    captured = {}

    def fake_publish(proxy_url, access_token, payload):
        captured["proxy_url"] = proxy_url
        captured["access_token"] = access_token
        captured["payload"] = payload
        return {"success": True, "update_id": "buffer-update-1"}

    monkeypatch.setattr("gateway.publish_queue.publish_to_buffer", fake_publish)

    response = client.post(
        "/api/publish/queue/manual-test",
        json={
            "account_id": "buffer-account-1",
            "profile_id": "profile-a",
            "video_id": "video-1",
            "brand_id": "brand-one",
            "copy_id": "copy-1",
            "scheduled_at": "2026-07-11T09:00:00+08:00",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["task"]["status"] == "success"
    assert captured["access_token"] == "token-buffer-account-1"
    assert captured["payload"] == {
        "text": "正文\n\n#tag",
        "profile_ids": ["profile-a"],
        "media": {"link": "https://cdn.example.com/one.mp4"},
        "scheduled_at": "2026-07-11T09:00:00+08:00",
    }
    assert captured["proxy_url"] == "socks5h://user2:pass2@203.0.113.8:9000"


def test_manual_publish_failure_keeps_video_available(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(
        db_path,
        "buffer-account-1",
        profile_ids='["profile-a"]',
    )
    write_json(
        data_dir / "videos.json",
        {
            "videos": [
                {
                    "id": "video-1",
                    "key": "videos/one.mp4",
                    "url": "https://cdn.example.com/one.mp4",
                    "used": False,
                }
            ]
        },
    )
    client.post("/api/content/brands", json={"brand": "Brand One"})
    client.post(
        "/api/content/copy",
        json={"brand_id": "brand-one", "body": "正文", "tags": ["#tag"]},
    )

    def fail_publish(proxy_url, access_token, payload):
        raise requests.exceptions.ReadTimeout("proxy timed out")

    monkeypatch.setattr("gateway.publish_queue.publish_to_buffer", fail_publish)

    response = client.post(
        "/api/publish/queue/manual-test",
        json={
            "account_id": "buffer-account-1",
            "profile_id": "profile-a",
            "video_id": "video-1",
            "brand_id": "brand-one",
            "copy_id": "copy-1",
        },
    )

    assert response.status_code == 502
    assert response.get_json()["task"]["status"] == "failed"
    assert response.get_json()["error"] == "proxy timed out"
    videos = client.get("/api/content/videos").get_json()
    assert videos["available_count"] == 1
    assert videos["used_count"] == 0


def test_batch_publish_uses_each_video_once(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1", profile_ids='["profile-a"]')
    insert_account(db_path, "buffer-account-2", profile_ids='["profile-b"]')
    write_json(
        data_dir / "videos.json",
        {
            "videos": [
                {"id": "video-1", "key": "one.mp4", "url": "https://cdn.example.com/one.mp4", "used": False},
                {"id": "video-2", "key": "two.mp4", "url": "https://cdn.example.com/two.mp4", "used": False},
            ]
        },
    )
    client.post("/api/content/brands", json={"brand": "Brand One"})
    client.post(
        "/api/content/copy",
        json={"brand_id": "brand-one", "body": "正文", "tags": "#tag"},
    )
    monkeypatch.setattr(
        "gateway.publish_queue.publish_to_buffer",
        lambda proxy_url, access_token, payload: {"success": True},
    )

    response = client.post(
        "/api/publish/queue/batch",
        json={"brand_id": "brand-one", "scheduled_at": "2026-07-11T09:00:00+08:00"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["created"] == 2
    assert sorted(task["video_id"] for task in payload["tasks"]) == ["video-1", "video-2"]

    second_response = client.post(
        "/api/publish/queue/batch",
        json={"brand_id": "brand-one", "scheduled_at": "2026-07-12T09:00:00+08:00"},
    )
    assert second_response.status_code == 200
    assert second_response.get_json()["created"] == 0
    assert second_response.get_json()["skipped"] == 2
    assert second_response.get_json()["skipped_reason"] == "not enough unused videos"


def test_batch_publish_only_enqueues_pending_tasks(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1", profile_ids='["profile-a"]')
    write_json(
        data_dir / "videos.json",
        {
            "videos": [
                {"id": "video-1", "key": "one.mp4", "url": "https://cdn.example.com/one.mp4", "used": False},
            ]
        },
    )
    client.post("/api/content/brands", json={"brand": "Brand One"})
    client.post(
        "/api/content/copy",
        json={"brand_id": "brand-one", "body": "Body", "tags": "#tag"},
    )
    calls = {"count": 0}

    def fake_publish(proxy_url, access_token, payload):
        calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr("gateway.publish_queue.publish_to_buffer", fake_publish)

    response = client.post(
        "/api/publish/queue/batch",
        json={"brand_id": "brand-one", "scheduled_at": "2026-07-11T09:00:00+08:00"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["created"] == 1
    assert payload["tasks"][0]["status"] == "pending"
    assert calls["count"] == 0
    assert read_json(data_dir / "videos.json", {"videos": []})["videos"][0]["used"] is True


def test_publish_queue_process_one_executes_single_pending_task(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1", profile_ids='["profile-a"]')
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "created_at": "2026-07-11T08:00:00+08:00",
                    "status": "pending",
                    "account_id": "buffer-account-1",
                    "profile_id": "profile-a",
                    "video_id": "video-1",
                    "video_url": "https://cdn.example.com/one.mp4",
                    "scheduled_at": "2026-07-11T09:00:00+08:00",
                    "proxy_session": "203.0.113.8:9000:user2:pass2",
                    "copy_text": "Body\n\n#tag",
                },
                {
                    "id": "task-2",
                    "created_at": "2026-07-11T08:01:00+08:00",
                    "status": "pending",
                    "account_id": "buffer-account-1",
                    "profile_id": "profile-a",
                    "video_id": "video-2",
                    "video_url": "https://cdn.example.com/two.mp4",
                    "scheduled_at": "2026-07-11T09:01:00+08:00",
                    "proxy_session": "203.0.113.8:9000:user2:pass2",
                    "copy_text": "Body\n\n#tag",
                },
            ]
        },
    )
    captured = {}

    def fake_publish(proxy_url, access_token, payload):
        captured["proxy_url"] = proxy_url
        captured["access_token"] = access_token
        captured["payload"] = payload
        return {"success": True, "update_id": "buffer-update-1"}

    monkeypatch.setattr("gateway.publish_queue.publish_to_buffer", fake_publish)

    response = client.post("/api/publish/queue/process-one")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["processed"] is True
    assert payload["task"]["id"] == "task-1"
    assert payload["task"]["status"] == "success"
    assert payload["task"]["buffer_update_id"] == "buffer-update-1"
    assert captured["access_token"] == "token-buffer-account-1"
    assert captured["payload"] == {
        "text": "Body\n\n#tag",
        "profile_ids": ["profile-a"],
        "media": {"link": "https://cdn.example.com/one.mp4"},
        "scheduled_at": "2026-07-11T09:00:00+08:00",
    }
    tasks = read_json(data_dir / "publish_tasks.json", {"tasks": []})["tasks"]
    assert [task["status"] for task in tasks] == ["success", "pending"]


def test_batch_publish_selected_accounts_creates_only_available_video_count(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1", profile_ids='["profile-a"]')
    insert_account(db_path, "buffer-account-2", profile_ids='["profile-b"]')
    insert_account(db_path, "buffer-account-3", profile_ids='["profile-c"]')
    write_json(
        data_dir / "videos.json",
        {
            "videos": [
                {"id": "video-1", "key": "one.mp4", "url": "https://cdn.example.com/one.mp4", "used": False},
                {"id": "video-2", "key": "two.mp4", "url": "https://cdn.example.com/two.mp4", "used": False},
            ]
        },
    )
    client.post("/api/content/brands", json={"brand": "Brand One"})
    client.post(
        "/api/content/copy",
        json={"brand_id": "brand-one", "body": "姝ｆ枃", "tags": "#tag"},
    )
    monkeypatch.setattr(
        "gateway.publish_queue.publish_to_buffer",
        lambda proxy_url, access_token, payload: {"success": True},
    )

    response = client.post(
        "/api/publish/queue/batch",
        json={
            "account_ids": [
                "buffer-account-1",
                "buffer-account-2",
                "buffer-account-3",
            ],
            "brand_id": "brand-one",
            "scheduled_at": "2026-07-11T09:00:00+08:00",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["requested"] == 3
    assert payload["created"] == 2
    assert payload["skipped"] == 1
    assert payload["skipped_reason"] == "not enough unused videos"
    assert [task["account_id"] for task in payload["tasks"]] == [
        "buffer-account-1",
        "buffer-account-2",
    ]
    assert [task["scheduled_at"] for task in payload["tasks"]] == [
        "2026-07-11T09:00:00+08:00",
        "2026-07-11T09:00:00+08:00",
    ]

    runs_response = client.get("/api/publish/queue/batches")
    assert runs_response.status_code == 200
    runs = runs_response.get_json()["runs"]
    assert runs[0]["requested"] == 3
    assert runs[0]["created"] == 2
    assert runs[0]["skipped"] == 1
    assert runs[0]["scheduled_at"] == "2026-07-11T09:00:00+08:00"
    assert runs[0]["account_ids"] == [
        "buffer-account-1",
        "buffer-account-2",
        "buffer-account-3",
    ]


def test_publish_results_masks_proxy_credentials(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "created_at": "2026-07-10T08:00:00+08:00",
                    "status": "failed",
                    "account_id": "buffer-account-1",
                    "profile_id": "profile-a",
                    "scheduled_at": "2026-07-11T09:00:00+08:00",
                    "proxy_session": "203.0.113.8:9000:user2:secret-pass",
                    "copy_text": "姝ｆ枃",
                    "error": "Buffer failed",
                }
            ]
        },
    )

    response = client.get("/api/publish/results")

    assert response.status_code == 200
    task = response.get_json()["tasks"][0]
    assert task["proxy_display"] == "203.0.113.8:9000"
    assert "secret-pass" not in response.get_data(as_text=True)


def test_publish_stats_returns_success_rows_with_engagement_fields(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1", profile_ids='["profile-a"]')
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "created_at": "2026-07-10T08:00:00+08:00",
                    "status": "success",
                    "account_id": "buffer-account-1",
                    "profile_id": "profile-a",
                    "scheduled_at": "2026-07-11T09:00:00+08:00",
                    "tiktok_url": "https://www.tiktok.com/@a/video/1",
                    "country": "US",
                    "views_24h": 101,
                    "likes_24h": 12,
                    "comments": 3,
                },
                {
                    "id": "task-2",
                    "created_at": "2026-07-10T09:00:00+08:00",
                    "status": "failed",
                    "account_id": "buffer-account-2",
                    "profile_id": "profile-b",
                    "error": "Buffer failed",
                },
            ]
        },
    )

    response = client.get("/api/publish/stats?date=2026-07-10")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 1
    assert payload["views_24h"] == 101
    assert payload["likes_24h"] == 12
    assert payload["comments"] == 3
    assert payload["engagement_count"] == 15
    assert payload["items"] == [
        {
            "id": "task-1",
            "account_id": "buffer-account-1",
            "account_name": "Account buffer-account-1",
            "profile_id": "profile-a",
            "tiktok_account_name": "@a",
            "scheduled_at": "2026-07-11T09:00:00+08:00",
            "tiktok_url": "https://www.tiktok.com/@a/video/1",
            "tiktok_url_short": "@a/video/1",
            "country": "US",
            "views_24h": 101,
            "likes_24h": 12,
            "comments": 3,
            "engagement_count": 15,
        }
    ]


def test_publish_stats_sorts_rows_and_uses_tiktok_account_name(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1", profile_ids='["profile-a"]')
    insert_account(db_path, "buffer-account-2", profile_ids='["profile-b"]')
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "low-comments",
                    "created_at": "2026-07-10T08:00:00+08:00",
                    "status": "success",
                    "account_id": "buffer-account-1",
                    "profile_id": "profile-a",
                    "tiktok_url": "https://www.tiktok.com/@a/video/1",
                    "views_24h": 200,
                    "likes_24h": 2,
                    "comments": 1,
                },
                {
                    "id": "high-comments",
                    "created_at": "2026-07-10T09:00:00+08:00",
                    "status": "success",
                    "account_id": "buffer-account-2",
                    "profile_id": "profile-b",
                    "tiktok_url": "https://www.tiktok.com/@b/video/1",
                    "views_24h": 10,
                    "likes_24h": 1,
                    "comments": 9,
                },
            ]
        },
    )

    payload = client.get("/api/publish/stats?sort=comments").get_json()

    assert [item["id"] for item in payload["items"]] == ["high-comments", "low-comments"]
    assert payload["items"][0]["tiktok_account_name"] == "@b"


def test_next_due_publish_sample_selects_successful_tiktok_task_after_24_hours(monkeypatch, tmp_path):
    _client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "too-new",
                    "status": "success",
                    "created_at": "2026-07-14T10:00:00+08:00",
                    "tiktok_url": "https://www.tiktok.com/@a/video/too-new",
                },
                {
                    "id": "due",
                    "status": "success",
                    "created_at": "2026-07-13T09:00:00+08:00",
                    "tiktok_url": "https://www.tiktok.com/@a/video/due",
                },
                {
                    "id": "done",
                    "status": "success",
                    "created_at": "2026-07-12T09:00:00+08:00",
                    "tiktok_url": "https://www.tiktok.com/@a/video/done",
                    "sample_status": "success",
                },
            ]
        },
    )

    task = next_due_publish_sample(
        data_dir,
        now=__import__("datetime").datetime.fromisoformat("2026-07-14T10:30:00+08:00"),
    )

    assert task["id"] == "due"


def test_next_pending_tiktok_link_backfill_selects_successful_buffer_task_without_url(monkeypatch, tmp_path):
    _client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "has-url",
                    "status": "success",
                    "tiktok_url": "https://www.tiktok.com/@a/video/1",
                    "buffer_update_id": "buffer-1",
                },
                {
                    "id": "due",
                    "status": "success",
                    "buffer_update_id": "buffer-2",
                },
            ]
        },
    )

    assert next_pending_tiktok_link_backfill(data_dir)["id"] == "due"


def test_next_pending_tiktok_link_backfill_skips_retry_cooldown(monkeypatch, tmp_path):
    _client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "cooling-down",
                    "status": "success",
                    "buffer_update_id": "buffer-1",
                    "link_backfill_status": "failed",
                    "link_backfill_next_attempt_at": "2026-07-14T10:30:00+08:00",
                },
                {
                    "id": "ready",
                    "status": "success",
                    "buffer_update_id": "buffer-2",
                    "link_backfill_status": "failed",
                    "link_backfill_next_attempt_at": "2026-07-14T09:30:00+08:00",
                },
            ]
        },
    )

    task = next_pending_tiktok_link_backfill(
        data_dir,
        now=__import__("datetime").datetime.fromisoformat("2026-07-14T10:00:00+08:00"),
    )

    assert task["id"] == "ready"


def test_mark_tiktok_link_backfill_success_and_failure(monkeypatch, tmp_path):
    _client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {"tasks": [{"id": "task-1", "status": "success"}]},
    )

    updated = mark_tiktok_link_backfill_success(
        data_dir,
        "task-1",
        "https://www.tiktok.com/@a/video/1",
    )

    assert updated["tiktok_url"].endswith("/1")
    assert updated["link_backfill_status"] == "success"
    assert updated["link_backfill_error"] == ""

    failed = mark_tiktok_link_backfill_failure(data_dir, "task-1", "not published")
    assert failed["link_backfill_status"] == "failed"
    assert failed["link_backfill_error"] == "not published"
    assert failed["link_backfill_next_attempt_at"]

    rate_limited = mark_tiktok_link_backfill_failure(
        data_dir,
        "task-1",
        "429 Client Error: Too Many Requests",
    )
    assert rate_limited["link_backfill_next_attempt_at"]


def test_mark_publish_sample_success_and_failure_write_sampling_fields(monkeypatch, tmp_path):
    _client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {"tasks": [{"id": "task-1", "status": "success"}]},
    )

    updated = mark_publish_sample_success(
        data_dir,
        "task-1",
        {
            "views_24h": "120",
            "likes_24h": "17",
            "comments": "4",
            "country": "US",
        },
    )

    assert updated["views_24h"] == 120
    assert updated["likes_24h"] == 17
    assert updated["comments"] == 4
    assert updated["country"] == "US"
    assert updated["sample_status"] == "success"
    assert updated["sample_error"] == ""

    failed = mark_publish_sample_failure(data_dir, "task-1", "captcha required")
    assert failed["sample_status"] == "failed"
    assert failed["sample_error"] == "captcha required"


def test_publish_results_stats_and_cleanup(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "created_at": "2026-07-10T08:00:00+08:00",
                    "status": "success",
                    "account_id": "buffer-account-1",
                    "profile_id": "profile-a",
                    "scheduled_at": "2026-07-11T09:00:00+08:00",
                    "proxy_session": "proxy-one",
                    "copy_text": "正文",
                    "tiktok_url": "https://www.tiktok.com/@a/video/1",
                    "views": 12,
                    "comments": 3,
                },
                {
                    "id": "task-2",
                    "created_at": "2026-07-09T08:00:00+08:00",
                    "status": "failed",
                    "account_id": "buffer-account-2",
                    "profile_id": "profile-b",
                    "scheduled_at": "2026-07-11T10:00:00+08:00",
                    "proxy_session": "proxy-two",
                    "copy_text": "正文",
                    "error": "Buffer failed",
                    "views": 0,
                    "comments": 0,
                },
            ]
        },
    )

    results = client.get("/api/publish/results?date=2026-07-10&status=success")
    stats = client.get("/api/publish/stats?date=2026-07-10")
    cleanup = client.post("/api/publish/logs/cleanup", json={"before_date": "2026-07-10"})

    assert results.status_code == 200
    assert results.get_json()["count"] == 1
    assert results.get_json()["tasks"][0]["tiktok_url"].endswith("/1")
    stats_payload = stats.get_json()
    assert stats_payload["count"] == 1
    assert stats_payload["success"] == 1
    assert stats_payload["failed"] == 0
    assert stats_payload["pending"] == 0
    assert stats_payload["views"] == 12
    assert stats_payload["views_24h"] == 12
    assert stats_payload["comments"] == 3
    assert stats_payload["items"][0]["tiktok_url"].endswith("/1")


def test_publish_sample_next_runs_sampler_and_updates_due_task(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "status": "success",
                    "created_at": "2026-07-10T08:00:00+08:00",
                    "profile_id": "profile-a",
                    "tiktok_url": "https://www.tiktok.com/@a/video/1",
                }
            ]
        },
    )

    class FakeCompleted:
        returncode = 0
        stdout = '{"views_24h": 123, "likes_24h": 14, "comments": 5}'
        stderr = ""

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return FakeCompleted()

    monkeypatch.setattr("gateway.publish_queue.subprocess.run", fake_run)

    response = client.post("/api/publish/sample-next")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["processed"] is True
    assert payload["task"]["views_24h"] == 123
    assert payload["task"]["likes_24h"] == 14
    assert payload["task"]["sample_status"] == "success"
    assert calls[0][0] == [
        "npm",
        "run",
        "tiktok-sampler",
        "--",
        "--profile-id",
        "profile-a",
        "--url",
        "https://www.tiktok.com/@a/video/1",
    ]


def test_publish_sample_next_can_override_min_age_for_automatic_runner(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "status": "success",
                    "created_at": "2026-07-14T08:00:00+08:00",
                    "profile_id": "profile-a",
                    "tiktok_url": "https://www.tiktok.com/@a/video/1",
                }
            ]
        },
    )

    class FakeCompleted:
        returncode = 0
        stdout = '{"views_24h": 7, "likes_24h": 2, "comments": 1}'
        stderr = ""

    monkeypatch.setattr("gateway.publish_queue.subprocess.run", lambda *args, **kwargs: FakeCompleted())

    response = client.post("/api/publish/sample-next?min_age_hours=0")

    assert response.status_code == 200
    assert response.get_json()["processed"] is True
    assert response.get_json()["task"]["views_24h"] == 7


def test_publish_auto_sample_tick_runs_backfill_and_sampling_with_config(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)
    calls = []

    def fake_backfill(data_dir, db_path):
        calls.append(("backfill", data_dir, db_path))
        return {"processed": False, "task": None}

    def fake_sample(data_dir, min_age_hours=24):
        calls.append(("sample", data_dir, min_age_hours))
        return {"processed": False, "task": None}

    monkeypatch.setattr("gateway.publish_queue.execute_next_tiktok_link_backfill", fake_backfill)
    monkeypatch.setattr("gateway.publish_queue.execute_next_publish_sample", fake_sample)

    response = client.post("/api/publish/auto-sample-tick")

    assert response.status_code == 200
    assert response.get_json()["enabled"] is True
    assert [call[0] for call in calls] == ["backfill", "sample"]
    assert calls[1][2] == 24


def test_publish_sample_next_records_sampler_failure(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "status": "success",
                    "created_at": "2026-07-10T08:00:00+08:00",
                    "profile_id": "profile-a",
                    "tiktok_url": "https://www.tiktok.com/@a/video/1",
                }
            ]
        },
    )

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "TikTok verification or captcha is visible"

    monkeypatch.setattr("gateway.publish_queue.subprocess.run", lambda *args, **kwargs: FakeCompleted())

    response = client.post("/api/publish/sample-next")

    assert response.status_code == 200
    task = response.get_json()["task"]
    assert task["sample_status"] == "failed"
    assert task["sample_error"] == "TikTok verification or captcha is visible"


def test_publish_backfill_link_next_fetches_buffer_post_and_writes_tiktok_url(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1")
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "status": "success",
                    "account_id": "buffer-account-1",
                    "buffer_update_id": "buffer-update-1",
                    "proxy_session": "203.0.113.8:9000:user2:pass2",
                }
            ]
        },
    )
    captured = {}

    def fake_fetch(access_token, post_id, proxy_url=""):
        captured["access_token"] = access_token
        captured["post_id"] = post_id
        captured["proxy_url"] = proxy_url
        return {
            "data": {
                "post": {
                    "serviceUpdateUrl": "https://www.tiktok.com/@a/video/1",
                }
            }
        }

    monkeypatch.setattr("gateway.publish_queue.fetch_buffer_post", fake_fetch)

    response = client.post("/api/publish/backfill-link-next")

    assert response.status_code == 200
    data = response.get_json()
    assert data["processed"] is True
    assert data["task"]["tiktok_url"] == "https://www.tiktok.com/@a/video/1"
    assert data["task"]["link_backfill_status"] == "success"
    assert captured["access_token"] == "token-buffer-account-1"
    assert captured["post_id"] == "buffer-update-1"


def test_publish_backfill_link_next_records_missing_buffer_url(monkeypatch, tmp_path):
    client, data_dir, db_path = make_client(tmp_path, monkeypatch)
    insert_account(db_path, "buffer-account-1")
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "status": "success",
                    "account_id": "buffer-account-1",
                    "buffer_update_id": "buffer-update-1",
                }
            ]
        },
    )
    monkeypatch.setattr("gateway.publish_queue.fetch_buffer_post", lambda *args, **kwargs: {"data": {"post": {"id": "buffer-update-1"}}})

    response = client.post("/api/publish/backfill-link-next")

    assert response.status_code == 200
    task = response.get_json()["task"]
    assert task["link_backfill_status"] == "failed"
    assert task["link_backfill_error"] == "Buffer post does not include a TikTok URL yet"


def test_daily_publish_schedule_can_be_saved(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/publish/schedule/daily",
        json={
            "enabled": True,
            "start_date": "2026-07-12",
            "time": "09:30",
            "brand_id": "brand-one",
            "account_ids": ["buffer-account-1", "buffer-account-2"],
        },
    )

    assert response.status_code == 200
    assert response.get_json()["schedule"] == {
        "id": "schedule-1",
        "enabled": True,
        "start_date": "2026-07-12",
        "time": "09:30",
        "brand_id": "brand-one",
        "account_ids": ["buffer-account-1", "buffer-account-2"],
        "account_count": 2,
        "updated_at": response.get_json()["schedule"]["updated_at"],
    }
    schedules_response = client.get("/api/publish/schedule/daily")
    assert schedules_response.status_code == 200
    assert schedules_response.get_json()["schedules"][0]["account_count"] == 2


def test_batch_publish_run_can_be_updated_and_deleted(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_batch_runs.json",
        {
            "runs": [
                {
                    "id": "batch-1",
                    "created_at": "2026-07-11T09:00:00+08:00",
                    "scheduled_at": "2026-07-12T09:00:00+08:00",
                    "brand_id": "brand-one",
                    "account_ids": ["account-1"],
                    "requested": 1,
                    "created": 1,
                    "skipped": 0,
                    "status": "created",
                }
            ]
        },
    )

    update = client.patch(
        "/api/publish/queue/batches/batch-1",
        json={
            "scheduled_at": "2026-07-13T10:30:00+08:00",
            "brand_id": "brand-two",
            "account_ids": ["account-2", "account-3"],
        },
    )
    delete = client.delete("/api/publish/queue/batches/batch-1")

    assert update.status_code == 200
    assert update.get_json()["run"]["scheduled_at"] == "2026-07-13T10:30:00+08:00"
    assert update.get_json()["run"]["brand_id"] == "brand-two"
    assert update.get_json()["run"]["account_ids"] == ["account-2", "account-3"]
    assert delete.status_code == 200
    assert delete.get_json()["deleted"] is True
    assert client.get("/api/publish/queue/batches").get_json()["runs"] == []


def test_daily_publish_schedule_can_be_updated_and_deleted(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_schedules.json",
        {
            "schedules": [
                {
                    "id": "schedule-1",
                    "enabled": True,
                    "start_date": "2026-07-12",
                    "time": "09:30",
                    "brand_id": "brand-one",
                    "account_ids": ["account-1"],
                    "account_count": 1,
                    "updated_at": "2026-07-11T09:00:00+08:00",
                }
            ]
        },
    )

    update = client.patch(
        "/api/publish/schedule/daily/schedule-1",
        json={
            "enabled": False,
            "start_date": "2026-07-14",
            "time": "11:45",
            "brand_id": "brand-two",
            "account_ids": ["account-2", "account-3"],
        },
    )
    delete = client.delete("/api/publish/schedule/daily/schedule-1")

    assert update.status_code == 200
    schedule = update.get_json()["schedule"]
    assert schedule["enabled"] is False
    assert schedule["start_date"] == "2026-07-14"
    assert schedule["time"] == "11:45"
    assert schedule["brand_id"] == "brand-two"
    assert schedule["account_ids"] == ["account-2", "account-3"]
    assert schedule["account_count"] == 2
    assert delete.status_code == 200
    assert delete.get_json()["deleted"] is True
    assert client.get("/api/publish/schedule/daily").get_json()["schedules"] == []


def test_publish_metrics_update_feed_stats(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    write_json(
        data_dir / "publish_tasks.json",
        {
            "tasks": [
                {
                    "id": "task-1",
                    "created_at": "2026-07-10T08:00:00+08:00",
                    "status": "success",
                    "views": 0,
                    "comments": 0,
                }
            ]
        },
    )

    response = client.post(
        "/api/publish/results/metrics",
        json={
            "task_id": "task-1",
            "views": 99,
            "comments": 8,
            "tiktok_url": "https://www.tiktok.com/@a/video/1",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["task"]["views"] == 99
    assert client.get("/api/publish/stats?date=2026-07-10").get_json()["comments"] == 8
