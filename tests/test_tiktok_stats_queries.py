from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tiktok_stats.db import connect_stats_db, migrate_stats_db
from tiktok_stats.queries import StatisticsQueryService
from tiktok_stats.store import StatsStore


METRICS = ("posts", "likes", "views", "comments")


@pytest.fixture
def query_db(tmp_path: Path) -> Path:
    path = tmp_path / "stats.db"
    with StatsStore(path) as store:
        accounts = [
            store.add_tracked_account("Alpha", "alpha"),
            store.add_tracked_account("bravo", "bravo"),
            store.add_tracked_account("CHARLIE", "charlie"),
            store.add_tracked_account("delta", "delta"),
            store.add_tracked_account("echo", "echo"),
            store.add_tracked_account("foxtrot", "foxtrot"),
        ]
        store.disable_account(accounts[4]["id"])

        # Previous day, requested start, and requested end. Values deliberately cover
        # positive, negative, zero, missing, first-day, and incomplete states.
        rows = {
            1: {
                "2026-07-19": ("ready", (10, 100, 1000, 20), (1, 10, 100, 2)),
                "2026-07-20": ("ready", (12, 120, 1200, 22), (2, 20, 200, 2)),
                "2026-07-21": ("ready", (15, 150, 1500, 25), (3, 30, 300, 3)),
            },
            2: {
                "2026-07-19": ("ready", (20, 200, 2000, 40), (0, 0, 0, 0)),
                "2026-07-20": ("ready", (20, 200, 2000, 40), (0, 0, 0, 0)),
                "2026-07-21": ("ready", (18, 180, 1800, 38), (-2, -20, -200, -2)),
            },
            3: {
                "2026-07-20": ("first_day", (5, 50, 500, 5), (None, None, None, None)),
                "2026-07-21": ("ready", (8, 80, 800, 8), (3, 30, 300, 3)),
            },
            4: {
                "2026-07-19": ("incomplete", (30, 300, 3000, 60), (None, None, None, None)),
                "2026-07-21": ("missing_previous", (31, 310, 3100, 61), (None, None, None, None)),
            },
            5: {
                "2026-07-19": ("ready", (40, 400, 4000, 80), (4, 40, 400, 8)),
                "2026-07-21": ("incomplete", (42, 420, 4200, 82), (None, None, None, None)),
            },
            6: {
                "2026-07-19": ("ready", (50, 500, 5000, 100), (5, 50, 500, 10)),
                "2026-07-21": ("ready", (53, 530, 5300, 103), (0, 0, 0, 0)),
            },
        }
        for account_id, dates in rows.items():
            for business_date, (status, totals, deltas) in dates.items():
                store.upsert_daily_metric(
                    account_id,
                    business_date,
                    baseline_status=status,
                    **{f"{name}_total": value for name, value in zip(METRICS, totals)},
                    **{f"{name}_delta": value for name, value in zip(METRICS, deltas)},
                )

        run_ok = store.start_run("full", scheduled_for="2026-07-21")
        store.finish_run(run_ok, "completed", details_json=json.dumps({"account_count": 1}))
        run_error = store.start_run("incremental", scheduled_for="2026-07-21T03:00:00Z")
        store.finish_run(
            run_error,
            "partial",
            details_json=json.dumps(
                {"errors": [{"account_id": 1, "code": "upstream", "message": "timeout"}]}
            ),
        )
        store.insert_snapshot(
            1,
            captured_at="2026-07-21T15:00:00.000Z",
            business_date="2026-07-21",
            run_id=run_ok,
            snapshot_type="full",
            coverage="full",
            post_count=15,
            likes_count=150,
            posts_view_count=1500,
            posts_comment_count=25,
        )
        store.replace_full_posts_with_deletions(
            1,
            [
                {"video_id": "v2", "created_at": "2026-07-21T02:00:00Z", "description": "new", "view_count": 20, "like_count": 2, "comment_count": 1, "share_count": 0},
                {"video_id": "v1", "created_at": "2026-07-20T02:00:00Z", "description": "old", "view_count": 10, "like_count": 1, "comment_count": 0, "share_count": 0},
            ],
            observed_at="2026-07-21T15:00:00.000Z",
        )
        store.replace_full_posts_with_deletions(
            1,
            [{"video_id": "v2", "created_at": "2026-07-21T02:00:00Z", "description": "new", "view_count": 21, "like_count": 2, "comment_count": 1, "share_count": 0}],
            observed_at="2026-07-22T15:00:00.000Z",
        )
        store.upsert_recent_posts(
            2,
            [{"video_id": "v3", "created_at": "2026-07-20T03:00:00Z", "description": "Bravo launch", "view_count": 99, "like_count": 8, "comment_count": None, "share_count": 0}],
            observed_at="2026-07-23T12:00:00.000Z",
        )
    return path


def _ids(result: dict) -> list[int]:
    return [row["account_id"] for row in result["rows"]]


def test_single_date_preserves_positive_negative_zero_and_missing(query_db: Path) -> None:
    with StatisticsQueryService(query_db) as service:
        result = service.query_account_table({"date": "2026-07-21"}, "posts_delta", "desc", 1, 20)

    rows = {row["account_id"]: row for row in result["rows"]}
    assert rows[1]["posts_delta"] == 3
    assert rows[2]["posts_delta"] == -2
    assert rows[6]["posts_delta"] == 0
    assert rows[4]["posts_delta"] is None
    assert rows[4]["baseline_status"] == "missing_previous"
    assert rows[5]["baseline_status"] == "incomplete"


def test_range_uses_end_totals_minus_immediately_previous_business_date(query_db: Path) -> None:
    with StatisticsQueryService(query_db) as service:
        result = service.query_account_table(
            {"start_date": "2026-07-20", "end_date": "2026-07-21"},
            "posts_delta",
            "desc",
            1,
            20,
        )

    rows = {row["account_id"]: row for row in result["rows"]}
    assert rows[1]["posts_delta"] == 5
    assert rows[2]["posts_delta"] == -2
    assert rows[6]["posts_delta"] == 3
    assert rows[3]["posts_delta"] is None
    assert rows[3]["baseline_status"] == "missing_previous"
    assert rows[4]["baseline_status"] == "incomplete_previous"
    assert rows[5]["baseline_status"] == "incomplete_end"


@pytest.mark.parametrize("metric", [f"{name}_delta" for name in METRICS])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_every_sort_is_stable_and_nulls_are_last(
    query_db: Path, metric: str, direction: str
) -> None:
    with StatisticsQueryService(query_db) as service:
        result = service.query_account_table({"date": "2026-07-21"}, metric, direction, 1, 20)

    values = [row[metric] for row in result["rows"]]
    first_null = next((index for index, value in enumerate(values) if value is None), len(values))
    assert all(value is not None for value in values[:first_null])
    assert all(value is None for value in values[first_null:])
    assert _ids(result) == (
        [2, 6, 1, 3, 4, 5] if direction == "asc" else [1, 3, 6, 2, 4, 5]
    )


def test_filters_and_pagination_boundaries(query_db: Path) -> None:
    with StatisticsQueryService(query_db) as service:
        disabled = service.query_account_table(
            {"date": "2026-07-21", "status": "disabled", "query": "ECHO", "baseline_status": "incomplete"},
            "posts_delta", "desc", 1, 2,
        )
        page_1 = service.query_account_table({"date": "2026-07-21"}, "posts_delta", "desc", 1, 2)
        page_3 = service.query_account_table({"date": "2026-07-21"}, "posts_delta", "desc", 3, 2)
        past_end = service.query_account_table({"date": "2026-07-21"}, "posts_delta", "desc", 4, 2)

    assert _ids(disabled) == [5]
    assert page_1["total"] == 6 and page_1["total_pages"] == 3
    assert len(page_1["rows"]) == 2 and len(page_3["rows"]) == 2
    assert past_end["rows"] == []


def test_summary_uses_exactly_the_filtered_table_semantics(query_db: Path) -> None:
    filters = {"start_date": "2026-07-20", "end_date": "2026-07-21", "status": "enabled"}
    with StatisticsQueryService(query_db) as service:
        table = service.query_account_table(filters, "views_delta", "asc", 1, 100)
        summary = service.query_summary(filters)

    assert summary["account_count"] == table["total"]
    for metric in (f"{name}_delta" for name in METRICS):
        assert summary[metric] == sum(row[metric] for row in table["rows"] if row[metric] is not None)
    assert summary["complete_count"] == sum(row["baseline_status"] == "ready" for row in table["rows"])


def test_summary_does_not_turn_an_all_null_metric_into_zero(query_db: Path) -> None:
    filters = {"date": "2026-07-21", "query": "delta"}
    with StatisticsQueryService(query_db) as service:
        summary = service.query_summary(filters)

    assert summary["posts_delta"] is None
    assert summary["likes_delta"] is None
    assert summary["views_delta"] is None
    assert summary["comments_delta"] is None


def test_detail_returns_totals_series_sorted_posts_deletions_and_account_history(query_db: Path) -> None:
    with StatisticsQueryService(query_db) as service:
        detail = service.query_account_detail(1, "2026-07-20", "2026-07-21")

    assert detail["account"]["username"] == "Alpha"
    assert detail["current_totals"] == {"posts": 15, "likes": 150, "views": 1500, "comments": 25}
    assert [row["business_date"] for row in detail["daily_series"]] == ["2026-07-20", "2026-07-21"]
    assert [post["video_id"] for post in detail["posts"]] == ["v2", "v1"]
    assert detail["posts"][1]["is_deleted"] is True
    assert detail["posts"][1]["deleted_detected_at"] == "2026-07-22T15:00:00.000Z"
    assert any(run["status"] == "completed" for run in detail["runs"])
    assert detail["errors"] == [{"run_id": 2, "code": "upstream", "message": "timeout"}]


def test_video_table_projects_latest_business_fields_and_summary(query_db: Path) -> None:
    with StatisticsQueryService(query_db) as service:
        result = service.query_video_table({}, "last_collected_at", "desc", 1, 50)

    assert [row["video_id"] for row in result["rows"]] == ["v3", "v2"]
    assert result["rows"][0] == {
        "video_id": "v3",
        "description": "Bravo launch",
        "account_id": 2,
        "username": "bravo",
        "published_at": "2026-07-20T03:00:00Z",
        "views": 99,
        "likes": 8,
        "comments": None,
        "last_collected_at": "2026-07-23T12:00:00.000Z",
    }
    assert result["summary"] == {
        "video_count": 2,
        "total_views": 120,
        "total_likes": 10,
        "total_comments": None,
    }


def test_video_table_filters_sorts_and_paginates_stably(query_db: Path) -> None:
    with StatisticsQueryService(query_db) as service:
        searched = service.query_video_table({"query": "BRAVO", "account_id": "2"}, "views", "desc", 1, 50)
        by_url = service.query_video_table({"query": "https://www.tiktok.com/@bravo/video/v3?lang=en"}, "views", "desc", 1, 50)
        dated = service.query_video_table(
            {"published_from": "2026-07-21", "published_to": "2026-07-21"},
            "published_at", "asc", 1, 50,
        )
        first = service.query_video_table({}, "views", "desc", 1, 1)
        second = service.query_video_table({}, "views", "desc", 2, 1)

    assert [row["video_id"] for row in searched["rows"]] == ["v3"]
    assert [row["video_id"] for row in by_url["rows"]] == ["v3"]
    assert [row["video_id"] for row in dated["rows"]] == ["v2"]
    assert first["total"] == 2 and first["total_pages"] == 2
    assert [first["rows"][0]["video_id"], second["rows"][0]["video_id"]] == ["v3", "v2"]


@pytest.mark.parametrize("sort", ["batch", "quality", "views; DROP TABLE posts_current"])
def test_video_table_rejects_unknown_sort(query_db: Path, sort: str) -> None:
    with StatisticsQueryService(query_db) as service:
        with pytest.raises(ValueError):
            service.query_video_table({}, sort, "desc", 1, 50)


def test_trend_matrix_switches_metrics_and_paginates_searched_accounts(query_db: Path) -> None:
    with StatisticsQueryService(query_db) as service:
        negative = service.query_trend_matrix("views_delta", "2026-07-20", "2026-07-21", "bravo", 1, 10)
        first = service.query_trend_matrix("comments_delta", "2026-07-20", "2026-07-21", "", 1, 2)
        second = service.query_trend_matrix("comments_delta", "2026-07-20", "2026-07-21", "", 2, 2)

    assert negative["accounts"] == [{"account_id": 2, "username": "bravo", "status": "enabled"}]
    assert negative["rows"] == [
        {"business_date": "2026-07-20", "values": {"2": 0}},
        {"business_date": "2026-07-21", "values": {"2": -200}},
    ]
    assert first["total_accounts"] == 6
    assert [a["account_id"] for a in first["accounts"]] == [1, 2]
    assert [a["account_id"] for a in second["accounts"]] == [3, 4]
    assert first["rows"][0]["values"]["1"] == 2


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("table_sort", ("posts_delta; DROP TABLE tracked_accounts", "asc")),
        ("table_direction", ("posts_delta", "desc; DELETE")),
        ("trend", ("views_delta UNION SELECT",)),
    ],
)
def test_sql_identifier_injection_is_rejected(query_db: Path, method: str, args: tuple[str, ...]) -> None:
    with StatisticsQueryService(query_db) as service:
        with pytest.raises(ValueError):
            if method.startswith("table"):
                service.query_account_table({"date": "2026-07-21"}, args[0], args[1], 1, 10)
            else:
                service.query_trend_matrix(args[0], "2026-07-20", "2026-07-21", "", 1, 10)


def test_opening_and_running_every_query_does_not_change_database(query_db: Path) -> None:
    before = _database_dump(query_db)
    service = StatisticsQueryService(query_db)
    after_open = _database_dump(query_db)
    service.query_summary({"date": "2026-07-21"})
    service.query_account_table({"date": "2026-07-21"}, "posts_delta", "desc", 1, 10)
    service.query_account_detail(1, "2026-07-20", "2026-07-21")
    service.query_trend_matrix("posts_delta", "2026-07-20", "2026-07-21", "", 1, 10)
    service.query_video_table({}, "last_collected_at", "desc", 1, 10)
    service.close()
    assert after_open == before
    assert _database_dump(query_db) == before


def test_opening_missing_database_does_not_create_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        StatisticsQueryService(missing)
    assert not missing.exists()


def _database_dump(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()
