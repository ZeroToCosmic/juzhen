from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import uuid
from pathlib import Path

from gateway.app import create_app
from tiktok_stats.secrets import CookieSecretStore
from tiktok_stats.store import StatsStore


class _FakeProtector:
    """Deterministic reversible protector used only for an isolated test path."""

    def protect(self, value: bytes) -> bytes:
        return b"test-cipher:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        prefix = b"test-cipher:"
        assert value.startswith(prefix)
        return value[len(prefix) :][::-1]


def _strategy_payload() -> dict:
    return {
        "strategies": [
            {
                "id": "restart-scroll",
                "name": "Restart scroll",
                "run_mode": "once",
                "batch_size": 1,
                "actions": [
                    {
                        "id": "scroll-one",
                        "type": "scroll_down",
                        "params": {
                            "distance": 420,
                            "total_count": [4, 9],
                            "burst_count": [2, 3],
                            "interval_seconds": [0.2, 0.5],
                        },
                    }
                ],
            }
        ]
    }


def _app_config(stats_path: Path, cookie_path: Path) -> dict:
    return {
        "TESTING": True,
        "TIKTOK_STATS_DB_PATH": stats_path,
        "TIKTOK_STATS_COOKIE_PATH": cookie_path,
        "TIKTOK_STATS_SECRET_STORE_FACTORY": lambda path: CookieSecretStore(
            path, protector=_FakeProtector()
        ),
    }


def _row_counts(path: Path) -> tuple[int, ...]:
    with closing(sqlite3.connect(path)) as connection, connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "tracked_accounts",
                "account_snapshots",
                "daily_account_metrics",
                "posts_current",
                "collection_runs",
            )
        )


def test_public_state_survives_flask_restart_without_plaintext_cookie(
    monkeypatch, tmp_path: Path, caplog
):
    config_path = tmp_path / "settings.json"
    stats_path = tmp_path / "stats" / "tiktok_stats.db"
    cookie_path = tmp_path / "stats" / "tiktok_cookie.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    secret = "session" + "id=" + uuid.uuid4().hex + uuid.uuid4().hex

    first_app = create_app(_app_config(stats_path, cookie_path))
    first_client = first_app.test_client()
    imported = first_client.post(
        "/api/tiktok-stats/accounts", json={"text": "@PersistCreator"}
    )
    strategy_saved = first_client.put(
        "/api/browser/strategies", json=_strategy_payload()
    )
    cookie_saved = first_client.put(
        "/api/tiktok-stats/settings/cookie", json={"cookie": secret}
    )
    assert imported.status_code == strategy_saved.status_code == cookie_saved.status_code == 200
    account_id = imported.get_json()["items"][0]["account"]["id"]

    with StatsStore(stats_path) as store:
        snapshot_id = store.insert_snapshot(
            account_id,
            captured_at="2026-07-22T15:00:00Z",
            business_date="2026-07-22",
            snapshot_type="full",
            coverage="full",
            post_count=8,
            posts_like_count=70,
            posts_view_count=900,
            posts_comment_count=15,
        )
        store.upsert_daily_metric(
            account_id,
            "2026-07-22",
            snapshot_id=snapshot_id,
            baseline_status="ready",
            posts_total=8,
            likes_total=70,
            views_total=900,
            comments_total=15,
            posts_delta=-1,
            likes_delta=5,
            views_delta=-20,
            comments_delta=2,
        )

    before_restart_counts = _row_counts(stats_path)
    del first_client, first_app

    restarted_app = create_app(_app_config(stats_path, cookie_path))
    restarted_client = restarted_app.test_client()
    accounts_response = restarted_client.get("/api/tiktok-stats/accounts")
    strategy_response = restarted_client.get("/api/browser/strategies")
    cookie_response = restarted_client.get("/api/tiktok-stats/settings/cookie")
    table_response = restarted_client.get(
        "/api/tiktok-stats/table?date=2026-07-22&sort=views_delta&direction=asc&page=1&page_size=50"
    )
    page_response = restarted_client.get("/tiktok-stats")

    assert all(
        response.status_code == 200
        for response in (
            accounts_response,
            strategy_response,
            cookie_response,
            table_response,
            page_response,
        )
    )
    assert [row["username"] for row in accounts_response.get_json()["accounts"]] == [
        "PersistCreator"
    ]
    params = strategy_response.get_json()["strategies"][0]["actions"][0]["params"]
    assert params["total_count"] == [4, 9]
    assert params["burst_count"] == [2, 3]
    assert cookie_response.get_json() == {
        "status": {
            "checked_at": None,
            "configured": True,
            "message": None,
            "state": "configured",
        }
    }
    row = table_response.get_json()["rows"][0]
    assert row["username"] == "PersistCreator"
    assert row["posts_delta"] == -1
    assert row["views_delta"] == -20
    assert _row_counts(stats_path) == before_restart_counts

    public_bytes = b"".join(
        response.data
        for response in (
            imported,
            strategy_saved,
            cookie_saved,
            accounts_response,
            strategy_response,
            cookie_response,
            table_response,
            page_response,
        )
    )
    assert secret.encode() not in public_bytes
    assert secret not in config_path.read_text(encoding="utf-8")
    assert secret.encode() not in stats_path.read_bytes()
    assert secret not in cookie_path.read_text(encoding="utf-8")
    assert secret not in caplog.text
    assert CookieSecretStore(cookie_path, protector=_FakeProtector()).load_cookie() == secret
    assert json.loads(config_path.read_text(encoding="utf-8"))["browser"][
        "block_strategies"
    ][0]["actions"][0]["params"] == params
