from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence


DELTA_METRICS = (
    "posts_delta",
    "likes_delta",
    "views_delta",
    "comments_delta",
)
_DIRECTIONS = {"asc": "ASC", "desc": "DESC"}
_ACCOUNT_STATUSES = {"all", "enabled", "disabled"}
_BASELINE_STATES = {
    "ready",
    "first_day",
    "missing_previous",
    "incomplete",
    "missing",
    "missing_end",
    "incomplete_end",
    "incomplete_previous",
}


class StatisticsQueryService:
    """Read-only projections over an already-migrated statistics database."""

    def __init__(
        self,
        source: str | Path | sqlite3.Connection | Any | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        store: Any | None = None,
    ) -> None:
        choices = sum(value is not None for value in (source, connection, store))
        if choices != 1:
            raise TypeError("provide exactly one database path, connection, or store")
        self._owns_connection = False
        if connection is not None:
            active = connection
        elif store is not None:
            active = store.connection
        elif isinstance(source, sqlite3.Connection):
            active = source
        elif hasattr(source, "connection"):
            active = source.connection
        else:
            path = Path(source)  # type: ignore[arg-type]
            if not path.is_file():
                raise FileNotFoundError(path)
            uri = path.resolve().as_uri() + "?mode=ro"
            active = sqlite3.connect(uri, uri=True, isolation_level=None)
            self._owns_connection = True
            active.execute("PRAGMA query_only = ON")
        active.row_factory = sqlite3.Row
        self.connection = active

    def __enter__(self) -> "StatisticsQueryService":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()
            self._owns_connection = False

    def query_summary(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        normalized = _normalize_filters(filters)
        rows = self._fetch_account_rows(normalized, None, None, None, None)
        result: dict[str, Any] = {
            "account_count": len(rows),
            "complete_count": sum(row["baseline_status"] == "ready" for row in rows),
            "incomplete_count": sum(row["baseline_status"] != "ready" for row in rows),
            "start_date": normalized["start_date"],
            "end_date": normalized["end_date"],
        }
        for metric in DELTA_METRICS:
            values = [row[metric] for row in rows if row[metric] is not None]
            result[metric] = sum(values) if values else None
        return result

    def query_account_table(
        self,
        filters: Mapping[str, Any] | None,
        sort: str,
        direction: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        sort_sql = _allowed_metric(sort, "sort")
        direction_sql = _allowed_direction(direction)
        page, page_size = _pagination(page, page_size)
        normalized = _normalize_filters(filters)
        total = self._count_account_rows(normalized)
        rows = self._fetch_account_rows(
            normalized,
            sort_sql,
            direction_sql,
            page_size,
            (page - 1) * page_size,
        )
        return {
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
            "start_date": normalized["start_date"],
            "end_date": normalized["end_date"],
        }

    def query_account_detail(
        self, account_id: int, start_date: str, end_date: str
    ) -> dict[str, Any]:
        account_id = _positive_int(account_id, "account_id")
        start, end = _date_range(start_date, end_date)
        account_row = self.connection.execute(
            "SELECT * FROM tracked_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account_row is None:
            raise KeyError(f"unknown tracked account: {account_id}")

        daily_series = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT business_date, baseline_status,
                       posts_total, likes_total, views_total, comments_total,
                       posts_delta, likes_delta, views_delta, comments_delta
                FROM daily_account_metrics
                WHERE account_id = ? AND business_date BETWEEN ? AND ?
                ORDER BY business_date ASC
                """,
                (account_id, start, end),
            )
        ]
        current_row = self.connection.execute(
            """
            SELECT posts_total, likes_total, views_total, comments_total
            FROM daily_account_metrics
            WHERE account_id = ? AND business_date <= ?
              AND baseline_status <> 'incomplete'
              AND posts_total IS NOT NULL AND likes_total IS NOT NULL
              AND views_total IS NOT NULL AND comments_total IS NOT NULL
            ORDER BY business_date DESC LIMIT 1
            """,
            (account_id, end),
        ).fetchone()
        current_totals = None
        if current_row is not None:
            current_totals = {
                name: current_row[f"{name}_total"]
                for name in ("posts", "likes", "views", "comments")
            }

        posts = []
        for row in self.connection.execute(
            """
            SELECT video_id, created_at, description, view_count, like_count,
                   comment_count, share_count, is_deleted, deleted_detected_at,
                   last_seen_at
            FROM posts_current WHERE account_id = ?
            ORDER BY created_at IS NULL ASC, created_at DESC, video_id ASC
            """,
            (account_id,),
        ):
            item = dict(row)
            item["is_deleted"] = bool(item["is_deleted"])
            posts.append(item)

        runs, errors = self._account_history(account_id)
        return {
            "account": dict(account_row),
            "current_totals": current_totals,
            "daily_series": daily_series,
            "posts": posts,
            "has_deleted_posts": any(post["is_deleted"] for post in posts),
            "runs": runs,
            "errors": errors,
            "start_date": start,
            "end_date": end,
        }

    def query_trend_matrix(
        self,
        metric: str,
        start_date: str,
        end_date: str,
        account_query: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        metric_sql = _allowed_metric(metric, "metric")
        start, end = _date_range(start_date, end_date)
        page, page_size = _pagination(page, page_size)
        search = _search_pattern(account_query)
        where = ""
        params: list[Any] = []
        if search is not None:
            where = "WHERE lower(username) LIKE ? ESCAPE '\\'"
            params.append(search)
        total = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM tracked_accounts {where}", params
            ).fetchone()[0]
        )
        account_rows = self.connection.execute(
            f"""
            SELECT id AS account_id, username, status
            FROM tracked_accounts {where}
            ORDER BY username_key ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        accounts = [dict(row) for row in account_rows]
        values: dict[tuple[str, int], int | None] = {}
        account_ids = [int(row["account_id"]) for row in accounts]
        if account_ids:
            placeholders = ", ".join("?" for _ in account_ids)
            for row in self.connection.execute(
                f"""
                SELECT business_date, account_id, {metric_sql} AS value
                FROM daily_account_metrics
                WHERE business_date BETWEEN ? AND ?
                  AND account_id IN ({placeholders})
                """,
                [start, end, *account_ids],
            ):
                values[(str(row["business_date"]), int(row["account_id"]))] = row["value"]
        matrix_rows = [
            {
                "business_date": business_date,
                "values": {
                    str(account_id): values.get((business_date, account_id))
                    for account_id in account_ids
                },
            }
            for business_date in _dates_inclusive(start, end)
        ]
        return {
            "metric": metric_sql,
            "start_date": start,
            "end_date": end,
            "accounts": accounts,
            "rows": matrix_rows,
            "page": page,
            "page_size": page_size,
            "total_accounts": total,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def _account_history(self, account_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        snapshot_run_ids = {
            int(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT run_id FROM account_snapshots WHERE account_id = ? AND run_id IS NOT NULL",
                (account_id,),
            )
        }
        runs: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for row in self.connection.execute(
            """
            SELECT id, run_type, status, started_at, finished_at, scheduled_for, details_json
            FROM collection_runs ORDER BY started_at DESC, id DESC
            """
        ):
            details = _json_object(row["details_json"])
            account_errors = [
                error
                for error in details.get("errors", [])
                if isinstance(error, dict) and error.get("account_id") == account_id
            ]
            if int(row["id"]) not in snapshot_run_ids and not account_errors:
                continue
            runs.append(
                {
                    "run_id": int(row["id"]),
                    "run_type": row["run_type"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "scheduled_for": row["scheduled_for"],
                }
            )
            errors.extend(
                {
                    "run_id": int(row["id"]),
                    "code": error.get("code"),
                    "message": error.get("message"),
                }
                for error in account_errors
            )
        return runs, errors

    def _row_components(
        self, normalized: Mapping[str, Any]
    ) -> tuple[str, str, list[Any]]:
        account_conditions: list[str] = []
        params: list[Any] = []
        status = normalized["status"]
        if status != "all":
            account_conditions.append("a.status = ?")
            params.append(status)
        if normalized["query"] is not None:
            account_conditions.append("lower(a.username) LIKE ? ESCAPE '\\'")
            params.append(normalized["query"])
        account_where = "WHERE " + " AND ".join(account_conditions) if account_conditions else ""

        start = normalized["start_date"]
        end = normalized["end_date"]
        if start == end:
            projection = """
                SELECT a.id AS account_id, a.username, a.username_key, a.status,
                       d.posts_total, d.likes_total, d.views_total, d.comments_total,
                       d.posts_delta, d.likes_delta, d.views_delta, d.comments_delta,
                       COALESCE(d.baseline_status, 'missing') AS baseline_status
                FROM tracked_accounts a
                LEFT JOIN daily_account_metrics d
                  ON d.account_id = a.id AND d.business_date = ?
            """
            params = [end, *params]
        else:
            previous = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
            complete = """
                e.id IS NOT NULL AND p.id IS NOT NULL
                AND e.baseline_status <> 'incomplete' AND p.baseline_status <> 'incomplete'
                AND e.posts_total IS NOT NULL AND e.likes_total IS NOT NULL
                AND e.views_total IS NOT NULL AND e.comments_total IS NOT NULL
                AND p.posts_total IS NOT NULL AND p.likes_total IS NOT NULL
                AND p.views_total IS NOT NULL AND p.comments_total IS NOT NULL
            """
            state = """
                CASE
                    WHEN e.id IS NULL THEN 'missing_end'
                    WHEN e.baseline_status = 'incomplete' OR e.posts_total IS NULL
                      OR e.likes_total IS NULL OR e.views_total IS NULL
                      OR e.comments_total IS NULL THEN 'incomplete_end'
                    WHEN p.id IS NULL THEN 'missing_previous'
                    WHEN p.baseline_status = 'incomplete' OR p.posts_total IS NULL
                      OR p.likes_total IS NULL OR p.views_total IS NULL
                      OR p.comments_total IS NULL THEN 'incomplete_previous'
                    ELSE 'ready'
                END
            """
            delta_columns = ",\n".join(
                f"CASE WHEN {complete} THEN e.{name}_total - p.{name}_total END AS {name}_delta"
                for name in ("posts", "likes", "views", "comments")
            )
            projection = f"""
                SELECT a.id AS account_id, a.username, a.username_key, a.status,
                       e.posts_total, e.likes_total, e.views_total, e.comments_total,
                       {delta_columns},
                       {state} AS baseline_status
                FROM tracked_accounts a
                LEFT JOIN daily_account_metrics e
                  ON e.account_id = a.id AND e.business_date = ?
                LEFT JOIN daily_account_metrics p
                  ON p.account_id = a.id AND p.business_date = ?
            """
            params = [end, previous, *params]
        return projection, account_where, params

    def _filtered_rows_sql(self, normalized: Mapping[str, Any]) -> tuple[str, list[Any]]:
        projection, account_where, params = self._row_components(normalized)
        baseline_states: Sequence[str] | None = normalized["baseline_statuses"]
        outer_where = ""
        if baseline_states:
            placeholders = ", ".join("?" for _ in baseline_states)
            outer_where = f"WHERE baseline_status IN ({placeholders})"
            params.extend(baseline_states)
        return f"WITH account_rows AS ({projection} {account_where}) SELECT * FROM account_rows {outer_where}", params

    def _count_account_rows(self, normalized: Mapping[str, Any]) -> int:
        sql, params = self._filtered_rows_sql(normalized)
        return int(self.connection.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0])

    def _fetch_account_rows(
        self,
        normalized: Mapping[str, Any],
        sort_sql: str | None,
        direction_sql: str | None,
        limit: int | None,
        offset: int | None,
    ) -> list[dict[str, Any]]:
        sql, params = self._filtered_rows_sql(normalized)
        if sort_sql is None:
            order = "ORDER BY account_id ASC"
        else:
            order = f"ORDER BY {sort_sql} IS NULL ASC, {sort_sql} {direction_sql}, account_id ASC"
        if limit is not None:
            sql = f"{sql} {order} LIMIT ? OFFSET ?"
            params.extend((limit, offset or 0))
        else:
            sql = f"{sql} {order}"
        return [dict(row) for row in self.connection.execute(sql, params)]


StatsQueryService = StatisticsQueryService


def _normalize_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(filters or {})
    unknown = set(values) - {
        "date",
        "start_date",
        "end_date",
        "status",
        "query",
        "baseline_status",
        "completeness",
    }
    if unknown:
        raise ValueError(f"unknown filters: {', '.join(sorted(unknown))}")
    single = values.get("date")
    start_value = values.get("start_date", single)
    end_value = values.get("end_date", single)
    if start_value is None or end_value is None:
        raise ValueError("date or start_date/end_date is required")
    start, end = _date_range(start_value, end_value)
    status = str(values.get("status", "all")).lower()
    if status not in _ACCOUNT_STATUSES:
        raise ValueError("status must be all, enabled, or disabled")
    baseline_value = values.get("baseline_status", values.get("completeness"))
    baseline_statuses = _baseline_filter(baseline_value)
    return {
        "start_date": start,
        "end_date": end,
        "status": status,
        "query": _search_pattern(values.get("query")),
        "baseline_statuses": baseline_statuses,
    }


def _baseline_filter(value: Any) -> tuple[str, ...] | None:
    if value is None or value == "all":
        return None
    raw = [value] if isinstance(value, str) else list(value)
    statuses = tuple(str(item) for item in raw)
    if not statuses or any(item not in _BASELINE_STATES for item in statuses):
        raise ValueError("invalid baseline/completeness status")
    return statuses


def _search_pattern(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    if not normalized:
        return None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _allowed_metric(value: str, label: str) -> str:
    if value not in DELTA_METRICS:
        raise ValueError(f"invalid {label}")
    return value


def _allowed_direction(value: str) -> str:
    try:
        return _DIRECTIONS[value.lower()]
    except (AttributeError, KeyError):
        raise ValueError("direction must be asc or desc") from None


def _pagination(page: int, page_size: int) -> tuple[int, int]:
    page = _positive_int(page, "page")
    page_size = _positive_int(page_size, "page_size")
    if page_size > 500:
        raise ValueError("page_size must not exceed 500")
    return page, page_size


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a positive integer") from None
    if parsed < 1 or parsed != value:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _date_range(start_value: Any, end_value: Any) -> tuple[str, str]:
    try:
        start = date.fromisoformat(str(start_value)).isoformat()
        end = date.fromisoformat(str(end_value)).isoformat()
    except ValueError:
        raise ValueError("dates must use YYYY-MM-DD") from None
    if start > end:
        raise ValueError("start_date must not be after end_date")
    return start, end


def _dates_inclusive(start: str, end: str) -> list[str]:
    current = date.fromisoformat(start)
    final = date.fromisoformat(end)
    result: list[str] = []
    while current <= final:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
