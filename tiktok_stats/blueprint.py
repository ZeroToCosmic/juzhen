"""Flask routes for the persistent TikTok statistics subsystem."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    render_template,
    request,
    session,
)
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from .imports import (
    existing_account_candidates,
    import_tracked_accounts,
    normalize_tiktok_username,
)
from .queries import StatisticsQueryService
from .secrets import CookieSecretStore, redact_secrets
from .store import StatsStore


_QUERY_KEY = "_tiktok_stats_query_service"
_STORE_KEY = "_tiktok_stats_store"
_MAX_PAGE_SIZE = 500


def create_tiktok_stats_blueprint() -> Blueprint:
    """Create routes that resolve all mutable dependencies from app configuration."""
    blueprint = Blueprint("tiktok_stats", __name__)

    @blueprint.teardown_request
    def close_request_resources(_error: BaseException | None) -> None:
        query = g.pop(_QUERY_KEY, None)
        if query is not None:
            query.close()
        store = g.pop(_STORE_KEY, None)
        if store is not None:
            store.close()

    @blueprint.after_request
    def redact_api_response(response: Response) -> Response:
        if request.path.startswith("/api/tiktok-stats/") and response.is_json:
            payload = response.get_json(silent=True)
            if payload is not None:
                response.set_data(current_app.json.dumps(redact_secrets(payload)))
                response.mimetype = "application/json"
        return response

    @blueprint.get("/tiktok-stats")
    def page() -> str:
        return render_template(
            "tiktok_stats.html",
            csrf_token=session["csrf_token"],
        )

    @blueprint.route("/api/tiktok-stats/accounts", methods=("GET", "POST"))
    def accounts():
        if request.method == "GET":
            _reject_unknown_query({"existing_query"})
            query = _query_service()
            rows = [
                dict(row)
                for row in query.connection.execute(
                    "SELECT * FROM tracked_accounts ORDER BY username_key, id"
                )
            ]
            candidates = _candidate_projection(request.args.get("existing_query"))
            return jsonify({"accounts": rows, "existing_candidates": candidates})

        body = _json_object()
        _allow_fields(body, {"text", "usernames"})
        if ("text" in body) == ("usernames" in body):
            return _error("invalid_import", "provide exactly one of text or usernames")
        values = body.get("text") if "text" in body else body.get("usernames")
        if "usernames" in body and not isinstance(values, list):
            return _error("invalid_import", "usernames must be an array")
        if "text" in body and not isinstance(values, str):
            return _error("invalid_import", "text must be text")
        if (isinstance(values, str) and not values.strip()) or values == []:
            return _error("invalid_import", "at least one username is required")
        result = import_tracked_accounts(_write_store(), values, source="manual")
        return jsonify(_public_import_result(result))

    @blueprint.post("/api/tiktok-stats/accounts/from-existing")
    def accounts_from_existing():
        body = _json_object()
        _allow_fields(body, {"candidate_ids"})
        candidate_ids = body.get("candidate_ids")
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or any(not isinstance(value, str) or not value for value in candidate_ids)
        ):
            return _error("invalid_candidate_id", "candidate_ids must be a non-empty string array")
        candidates = {item["candidate_id"]: item for item in _candidate_projection(None)}
        if len(set(candidate_ids)) != len(candidate_ids) or any(
            candidate_id not in candidates for candidate_id in candidate_ids
        ):
            return _error("invalid_candidate_id", "one or more candidate IDs are invalid")
        selected = [candidates[candidate_id] for candidate_id in candidate_ids]
        result = import_tracked_accounts(
            _write_store(),
            [item["username"] for item in selected],
            source="existing_accounts",
            source_ids=[item["source_account_id"] for item in selected],
        )
        return jsonify(_public_import_result(result))

    @blueprint.patch("/api/tiktok-stats/accounts/<int:account_id>")
    def patch_account(account_id: int):
        if account_id < 1:
            return _error("invalid_account_id", "account ID must be positive")
        body = _json_object()
        _allow_fields(body, {"username", "enabled"})
        if not body:
            return _error("invalid_update", "at least one update is required")
        if "enabled" in body and not isinstance(body["enabled"], bool):
            return _error("invalid_enabled", "enabled must be a boolean")
        store = _write_store()
        account = store.account_by_id(account_id)
        if account is None:
            return _error("account_not_found", "tracked account not found", 404)
        try:
            if "username" in body:
                try:
                    username, username_key = normalize_tiktok_username(body["username"])
                except ValueError as error:
                    return _error("invalid_username", str(error))
                account = store.update_account_identity(account_id, username, username_key)
            if "enabled" in body:
                (store.enable_account if body["enabled"] else store.disable_account)(account_id)
                account = store.account_by_id(account_id)
        except sqlite3.IntegrityError:
            return _error("username_conflict", "username is already tracked", 409)
        return jsonify({"account": account})

    @blueprint.route("/api/tiktok-stats/settings/cookie", methods=("GET", "PUT"))
    def cookie_settings():
        secret_store = _secret_store()
        if request.method == "GET":
            return jsonify({"status": secret_store.public_status()})
        body = _json_object()
        _allow_fields(body, {"cookie"})
        value = body.get("cookie")
        if not isinstance(value, str) or not value.strip():
            return _error("invalid_cookie", "Cookie must be non-empty text")
        status = secret_store.save_cookie(value.strip())
        return jsonify({"status": status.as_public_dict()})

    @blueprint.post("/api/tiktok-stats/settings/cookie/validate")
    def validate_cookie():
        body = _json_object()
        _allow_fields(body, set())
        secret_store = _secret_store()
        cookie, cookie_version = secret_store.load_cookie_with_version()
        if not cookie:
            return _error("cookie_not_configured", "Cookie is not configured", 409)
        valid = False
        try:
            result = _configured_callable("TIKTOK_STATS_COOKIE_VALIDATOR")(cookie)
            if isinstance(result, Mapping):
                valid = result.get("valid") is True
            elif isinstance(result, tuple) and result:
                valid = result[0] is True
            else:
                valid = result is True
        except Exception:
            valid = False
        secret_store.mark_validation(
            valid,
            "validation succeeded" if valid else "validation failed",
            datetime.now(UTC),
            expected_version=cookie_version,
        )
        return jsonify({"status": secret_store.public_status()})

    @blueprint.get("/api/tiktok-stats/status")
    def status():
        _reject_unknown_query(set())
        public_status = _configured_callable("TIKTOK_STATS_STATUS_PROVIDER")()
        if not isinstance(public_status, Mapping):
            public_status = {}
        return jsonify(
            {
                **dict(public_status),
                "configured": _secret_store().public_status().get("configured", False),
            }
        )

    @blueprint.route("/api/tiktok-stats/runs", methods=("GET", "POST"))
    def runs():
        if request.method == "GET":
            try:
                _reject_unknown_query({"page", "page_size"})
                page = _query_int("page", 1)
                page_size = _query_int("page_size", 50, maximum=_MAX_PAGE_SIZE)
            except ValueError as error:
                return _error("invalid_query", str(error))
            connection = _query_service().connection
            total = int(connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0])
            rows = connection.execute(
                """
                SELECT id, run_type, status, started_at, finished_at, scheduled_for, details_json
                FROM collection_runs ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (page_size, (page - 1) * page_size),
            )
            return jsonify(
                {
                    "runs": [_public_run(row) for row in rows],
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                }
            )

        body = _json_object()
        _allow_fields(body, {"run_type", "account_ids"})
        run_type = body.get("run_type")
        if run_type not in {"incremental", "full"}:
            return _error("invalid_run_type", "run_type must be incremental or full")
        try:
            account_ids = _positive_id_list(body.get("account_ids"))
        except ValueError as error:
            return _error("invalid_account_ids", str(error))
        if account_ids is not None:
            placeholders = ", ".join("?" for _ in account_ids)
            found_ids = {
                int(row[0])
                for row in _query_service().connection.execute(
                    f"SELECT id FROM tracked_accounts WHERE status = 'enabled' AND id IN ({placeholders})",
                    account_ids,
                )
            }
            if found_ids != set(account_ids):
                return _error(
                    "invalid_account_ids", "account_ids must reference enabled tracked accounts"
                )
        try:
            result = _configured_callable("TIKTOK_STATS_RUN_DISPATCHER")(
                run_type, account_ids=account_ids
            )
        except Exception:
            return _error("dispatch_unavailable", "manual run dispatch is unavailable", 503)
        if not isinstance(result, Mapping) or not result.get("run_id"):
            return _error("dispatch_failed", "manual run was not durably enqueued", 503)
        return jsonify({"run": dict(result)}), 202

    @blueprint.get("/api/tiktok-stats/summary")
    def summary():
        try:
            _reject_unknown_query(_FILTER_KEYS)
            return jsonify(_query_service().query_summary(_filters()))
        except ValueError as error:
            return _error("invalid_query", str(error))

    @blueprint.get("/api/tiktok-stats/table")
    def table():
        try:
            _reject_unknown_query(_FILTER_KEYS | {"sort", "direction", "page", "page_size"})
            result = _query_service().query_account_table(
                _filters(),
                request.args.get("sort", "posts_delta"),
                request.args.get("direction", "desc"),
                _query_int("page", 1),
                _query_int("page_size", 50, maximum=_MAX_PAGE_SIZE),
            )
            return jsonify(result)
        except ValueError as error:
            return _error("invalid_query", str(error))

    @blueprint.get("/api/tiktok-stats/accounts/<int:account_id>/detail")
    def detail(account_id: int):
        try:
            if account_id < 1:
                raise ValueError("account ID must be positive")
            _reject_unknown_query({"start_date", "end_date"})
            result = _query_service().query_account_detail(
                account_id, request.args.get("start_date"), request.args.get("end_date")
            )
            return jsonify(result)
        except ValueError as error:
            return _error("invalid_query", str(error))
        except KeyError:
            return _error("account_not_found", "tracked account not found", 404)

    @blueprint.get("/api/tiktok-stats/trends")
    def trends():
        try:
            _reject_unknown_query(
                {"metric", "start_date", "end_date", "query", "page", "page_size"}
            )
            result = _query_service().query_trend_matrix(
                request.args.get("metric", "posts_delta"),
                request.args.get("start_date"),
                request.args.get("end_date"),
                request.args.get("query"),
                _query_int("page", 1),
                _query_int("page_size", 50, maximum=_MAX_PAGE_SIZE),
            )
            return jsonify(result)
        except ValueError as error:
            return _error("invalid_query", str(error))

    return blueprint


_FILTER_KEYS = {
    "date", "start_date", "end_date", "status", "query",
    "baseline_status", "completeness",
}


def _query_service() -> StatisticsQueryService:
    service = g.get(_QUERY_KEY)
    if service is None:
        factory = current_app.config["TIKTOK_STATS_QUERY_FACTORY"]
        service = factory(current_app.config["TIKTOK_STATS_DB_PATH"])
        setattr(g, _QUERY_KEY, service)
    return service


def _write_store() -> StatsStore:
    store = g.get(_STORE_KEY)
    if store is None:
        factory = current_app.config["TIKTOK_STATS_STORE_FACTORY"]
        store = factory(current_app.config["TIKTOK_STATS_DB_PATH"])
        setattr(g, _STORE_KEY, store)
    return store


def _secret_store() -> CookieSecretStore:
    factory = current_app.config["TIKTOK_STATS_SECRET_STORE_FACTORY"]
    return factory(current_app.config["TIKTOK_STATS_COOKIE_PATH"])


def _configured_callable(key: str) -> Callable[..., Any]:
    value = current_app.config.get(key)
    if not callable(value):
        raise RuntimeError(f"{key} is not configured")
    return value


def _json_object() -> dict[str, Any]:
    try:
        payload = request.get_json(silent=False)
    except (BadRequest, UnsupportedMediaType):
        raise _ApiError("invalid_json", "request body must be valid JSON") from None
    if not isinstance(payload, dict):
        raise _ApiError("json_object_required", "JSON body must be an object")
    return payload


def _allow_fields(body: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _ApiError("unknown_field", f"unknown field: {unknown[0]}")


def _reject_unknown_query(allowed: set[str]) -> None:
    unknown = sorted(set(request.args) - allowed)
    if unknown:
        raise _ApiError("invalid_query", f"unknown query parameter: {unknown[0]}")


def _filters() -> dict[str, str]:
    return {key: request.args[key] for key in _FILTER_KEYS if key in request.args}


def _query_int(name: str, default: int, *, maximum: int | None = None) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if value < 1 or str(value) != raw:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")
    return value


def _positive_id_list(value: Any) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value
    ):
        raise ValueError("account_ids must be an array of positive integers")
    if len(set(value)) != len(value):
        raise ValueError("account_ids must not contain duplicates")
    return value


def _candidate_projection(query: str | None) -> list[dict[str, Any]]:
    path = current_app.config["TIKTOK_STATS_EXISTING_ACCOUNTS_DB_PATH"]
    candidates = existing_account_candidates(path, query)
    for candidate in candidates:
        candidate["candidate_id"] = ":".join(
            (candidate["source_account_id"], candidate["channel_id"], candidate["username_key"])
        )
    return candidates


def _public_import_result(result) -> dict[str, Any]:
    return {
        "summary": {
            "added": result.added,
            "existing": result.existing,
            "reactivated": result.reactivated,
            "invalid": result.invalid,
        },
        "items": [
            {
                "value": item.value,
                "status": item.status,
                "account": item.account,
                "error": item.error,
            }
            for item in result.items
        ],
    }


def _public_run(row) -> dict[str, Any]:
    try:
        details = json.loads(row["details_json"] or "{}")
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    return {
        "run_id": int(row["id"]),
        "run_type": row["run_type"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "scheduled_for": row["scheduled_for"],
        "details": details,
    }


class _ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _error(code: str, message: str, status: int = 400):
    return jsonify({"error": {"code": code, "message": message}}), status


def register_tiktok_stats_error_handler(app) -> None:
    @app.errorhandler(_ApiError)
    def handle_api_error(error: _ApiError):
        return _error(error.code, error.message, error.status)


def default_query_factory(path: str | Path) -> StatisticsQueryService:
    return StatisticsQueryService(path)


def default_store_factory(path: str | Path) -> StatsStore:
    return StatsStore(path)


def default_secret_store_factory(path: str | Path) -> CookieSecretStore:
    return CookieSecretStore(path)


def unavailable_cookie_validator(_cookie: str) -> bool:
    return False


def unavailable_run_dispatcher(_run_type: str, *, account_ids=None):
    del account_ids
    raise RuntimeError("manual run dispatcher is unavailable")


def default_status_provider() -> dict[str, Any]:
    return {"scraper": {"running": False}, "worker": {"running": False}}
