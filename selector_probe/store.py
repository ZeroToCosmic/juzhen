from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import uuid

from browser_element_schema import ELEMENT_SCOPES, normalize_element_definitions


SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_for TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    attempt_token TEXT NOT NULL DEFAULT '',
    active_version_before TEXT NOT NULL DEFAULT '',
    published_version_after TEXT NOT NULL DEFAULT '',
    failed_aliases_json TEXT NOT NULL DEFAULT '[]',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS element_probe_contracts (
    alias TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT 'tiktok',
    environment TEXT NOT NULL DEFAULT 'production',
    contract_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(site, environment, alias)
);
CREATE TABLE IF NOT EXISTS selector_validation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_run_id INTEGER NOT NULL REFERENCES probe_runs(id) ON DELETE CASCADE,
    profile_mask TEXT NOT NULL,
    round_number INTEGER NOT NULL CHECK (round_number IN (1, 2)),
    page_state TEXT NOT NULL,
    result TEXT NOT NULL,
    failure_code TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    screenshot_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validation_probe_run
ON selector_validation_runs(probe_run_id, profile_mask, round_number);
CREATE TABLE IF NOT EXISTS selector_versions (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    environment TEXT NOT NULL,
    probe_run_id INTEGER REFERENCES probe_runs(id),
    lease_owner TEXT NOT NULL DEFAULT '',
    element_request_id TEXT NOT NULL DEFAULT '',
    element_request_generation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    base_version_id TEXT NOT NULL DEFAULT '',
    bundle_json TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS publication_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    element_request_id TEXT NOT NULL DEFAULT '',
    element_request_generation INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claim_generation INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    claim_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS strategy_dependencies (
    alias TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    strategy_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(alias, strategy_id, action_id)
);
CREATE TABLE IF NOT EXISTS strategy_gate_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('probe', 'manual')),
    site TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    selector_version_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    cleared_at TEXT,
    cleared_by TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_open_gate_reason
ON strategy_gate_reasons(
    strategy_id,
    source,
    reason_code,
    selector_version_id
)
WHERE cleared_at IS NULL;
CREATE TABLE IF NOT EXISTS strategy_gate_revisions (
    strategy_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
CREATE TABLE IF NOT EXISTS probe_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT 'unknown',
    environment TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL CHECK (status IN ('open', 'acknowledged', 'resolved')),
    failure_class TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    strategy_ids_json TEXT NOT NULL,
    active_version TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    details_json TEXT NOT NULL,
    screenshot_path TEXT NOT NULL DEFAULT '',
    acknowledged_at TEXT,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_open_probe_alert
ON probe_alerts(fingerprint)
WHERE status IN ('open', 'acknowledged');
CREATE TABLE IF NOT EXISTS probe_alert_screenshots (
    alert_id INTEGER PRIMARY KEY
        REFERENCES probe_alerts(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_alert_screenshot_created
ON probe_alert_screenshots(created_at);
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL
        REFERENCES probe_alerts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    claim_token TEXT NOT NULL DEFAULT '',
    claim_generation INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_due
ON webhook_outbox(status, next_attempt_at, lease_until);
CREATE TABLE IF NOT EXISTS probe_health_state (
    site TEXT NOT NULL,
    environment TEXT NOT NULL,
    failure_started_at TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_retry_at TEXT NOT NULL DEFAULT '',
    last_validated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(site, environment)
);
CREATE TABLE IF NOT EXISTS probe_effect_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_key TEXT NOT NULL UNIQUE,
    site TEXT NOT NULL DEFAULT 'tiktok',
    environment TEXT NOT NULL DEFAULT 'production',
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'selector_failure',
            'probe_unavailable',
            'probe_stale',
            'recovery'
        )
    ),
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'store_applied', 'completed')
    ),
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_effect_outbox_pending
ON probe_effect_outbox(status, id);
CREATE TABLE IF NOT EXISTS managed_elements (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    management_source TEXT NOT NULL CHECK (
        management_source IN ('automatic', 'legacy_manual', 'disabled')
    ),
    published_status TEXT NOT NULL CHECK (
        published_status IN (
            'healthy', 'using_lkg', 'failed', 'probe_unavailable', 'disabled'
        )
    ),
    draft_status TEXT CHECK (
        draft_status IS NULL OR draft_status IN (
            'draft', 'queued', 'probing', 'validating'
        )
    ),
    active_version_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    primary_locator_type TEXT NOT NULL DEFAULT '',
    last_validated_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS element_drafts (
    element_id TEXT PRIMARY KEY
        REFERENCES managed_elements(id) ON DELETE CASCADE,
    contract_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    validation_json TEXT NOT NULL DEFAULT '{}',
    base_version_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by INTEGER NOT NULL CHECK (created_by > 0),
    created_by_username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_managed_elements_health
ON managed_elements(published_status, draft_status, last_validated_at);
CREATE TABLE IF NOT EXISTS element_catalog_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT OR IGNORE INTO element_catalog_state(singleton, revision) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS selector_management_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER NOT NULL CHECK (actor_user_id > 0),
    actor_username TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    result TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selector_management_audit_created
ON selector_management_audit_events(created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS management_resource_revisions (
    resource TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
CREATE TABLE IF NOT EXISTS management_idempotency_cache (
    actor_user_id INTEGER NOT NULL CHECK (actor_user_id > 0),
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'completed' CHECK (
        state IN ('pending', 'completed', 'failed')
    ),
    request_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(actor_user_id, operation, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_management_idempotency_expiry
ON management_idempotency_cache(expires_at);
CREATE TABLE IF NOT EXISTS management_settings_publications (
    id TEXT PRIMARY KEY,
    actor_user_id INTEGER NOT NULL CHECK (actor_user_id > 0),
    actor_username TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
    staged_revision INTEGER NOT NULL CHECK (staged_revision >= 1),
    candidate_json TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    private_reference TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'completed', 'failed')
    ),
    error_code TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(actor_user_id, operation, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_management_settings_publications_pending
ON management_settings_publications(status, staged_revision, created_at);
CREATE TABLE IF NOT EXISTS management_run_requests (
    id TEXT PRIMARY KEY,
    actor_user_id INTEGER NOT NULL CHECK (actor_user_id > 0),
    actor_username TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual' CHECK (
        trigger IN ('manual', 'scheduled', 'retry')
    ),
    retry_of_run_id TEXT NOT NULL DEFAULT '',
    probe_run_id INTEGER REFERENCES probe_runs(id),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'completed', 'failed', 'dispatch_failed'
        )
    ),
    failure_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_management_run_requests_created
ON management_run_requests(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_management_run_requests_active
ON management_run_requests((1))
WHERE status IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_management_run_requests_probe_run
ON management_run_requests(probe_run_id)
WHERE probe_run_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS management_preflight_health (
    workspace TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS element_request_outbox (
    request_id TEXT PRIMARY KEY,
    request_type TEXT NOT NULL CHECK (
        request_type IN ('probe', 'validate')
    ),
    element_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
    contract_json TEXT NOT NULL,
    actor_user_id INTEGER NOT NULL CHECK (actor_user_id > 0),
    actor_username TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'processing', 'publishing', 'completed', 'failed'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claim_token TEXT NOT NULL DEFAULT '',
    claim_generation INTEGER NOT NULL DEFAULT 0
        CHECK (claim_generation >= 0),
    lease_until TEXT,
    next_attempt_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    staged_version_id TEXT NOT NULL DEFAULT '',
    staged_result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_element_request_outbox_claim
ON element_request_outbox(status, next_attempt_at, lease_until, created_at);
"""

_KEY_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_MASK = re.compile(r"^\*\*\*(?:.{4})?$", re.DOTALL)
_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INFRASTRUCTURE_RETRY_SECONDS = (900, 1800, 3600)
ELEMENT_REQUEST_RETRY_SECONDS = (15, 30, 60)
MANAGEMENT_PENDING_LEASE_SECONDS = 300


class ElementAlreadyExistsError(RuntimeError):
    pass


class ElementHasDependenciesError(RuntimeError):
    pass


class ElementMigrationConflictError(RuntimeError):
    pass


class ElementNotFoundError(LookupError):
    pass


class ElementRequestConflictError(RuntimeError):
    pass


class ElementRequestInProgressError(RuntimeError):
    pass


class StaleElementRevisionError(RuntimeError):
    pass


class StaleManagementRevisionError(RuntimeError):
    pass


class GateStillActiveError(RuntimeError):
    pass


class ManagementIdempotencyConflictError(RuntimeError):
    pass


def _json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value must be JSON-safe") from error


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as error:
        raise ValueError("value must be JSON-safe") from error


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


_PRIVATE_SETTINGS_KEYS = frozenset(
    {
        "account_token",
        "api_key",
        "password",
        "raw",
        "secret",
        "secret_access_key",
        "signing_secret",
        "token",
    }
)


def _private_safe_settings_candidate(
    value: Mapping[str, object],
) -> dict[str, object]:
    def inspect(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized = str(key).strip().lower()
                if normalized in _PRIVATE_SETTINGS_KEYS:
                    raise ValueError(
                        "settings publication candidate contains private data"
                    )
                inspect(child)
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for child in item:
                inspect(child)

    candidate = dict(value)
    inspect(candidate)
    _canonical_json(candidate)
    return candidate


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _scheduled_slot(value: object) -> tuple[str, datetime]:
    text = _required_text(value, "scheduled_for")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("scheduled_for must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("scheduled_for must include a timezone offset")
    normalized = parsed.astimezone(UTC)
    return normalized.isoformat(), normalized


def _iso_timestamp(value: object, name: str = "timestamp") -> str:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return parsed.astimezone(UTC).isoformat()


class SelectorProbeStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        legacy_management_table = self.connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'management_run_requests'
            """
        ).fetchone()
        if legacy_management_table is not None:
            self._migrate_management_run_lifecycle()
        self.connection.executescript(SCHEMA)
        self._migrate_contract_scope_key()
        self._migrate_alert_context()
        self._migrate_attempt_token()
        self._migrate_outbox_claims()
        self._migrate_publication_fence()
        self._migrate_gate_reason_scope()
        self._migrate_probe_effect_scope()
        self._migrate_strategy_dependency_name()
        self._migrate_element_catalog_schema()
        self._migrate_element_publication_workflow()
        self._migrate_management_idempotency()
        self._migrate_management_run_lifecycle()

    def __enter__(self) -> SelectorProbeStore:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.connection.close()

    def _migrate_management_run_lifecycle(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(management_run_requests)"
            ).fetchall()
        }
        required = {
            "trigger",
            "probe_run_id",
            "failure_code",
            "finished_at",
        }
        table_sql_row = self.connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'management_run_requests'
            """
        ).fetchone()
        table_sql = str(table_sql_row["sql"] or "") if table_sql_row else ""
        if required.issubset(columns) and "'queued'" in table_sql:
            return
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "DROP INDEX IF EXISTS idx_management_run_requests_created"
            )
            self.connection.execute(
                "DROP INDEX IF EXISTS idx_management_run_requests_active"
            )
            self.connection.execute(
                "DROP INDEX IF EXISTS idx_management_run_requests_probe_run"
            )
            self.connection.execute(
                """
                ALTER TABLE management_run_requests
                RENAME TO management_run_requests_legacy
                """
            )
            self.connection.execute(
                """
                CREATE TABLE management_run_requests (
                    id TEXT PRIMARY KEY,
                    actor_user_id INTEGER NOT NULL
                        CHECK (actor_user_id > 0),
                    actor_username TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT 'manual' CHECK (
                        trigger IN ('manual', 'scheduled', 'retry')
                    ),
                    retry_of_run_id TEXT NOT NULL DEFAULT '',
                    probe_run_id INTEGER REFERENCES probe_runs(id),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'queued', 'running', 'completed', 'failed',
                            'dispatch_failed'
                        )
                    ),
                    failure_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            legacy_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(management_run_requests_legacy)"
                ).fetchall()
            }
            if legacy_columns:
                self.connection.execute(
                    """
                    INSERT INTO management_run_requests (
                        id, actor_user_id, actor_username, trigger,
                        retry_of_run_id, status, failure_code, created_at,
                        updated_at, finished_at
                    )
                    SELECT id, actor_user_id, actor_username,
                           CASE WHEN retry_of_run_id <> ''
                                THEN 'retry' ELSE 'manual' END,
                           retry_of_run_id,
                           CASE WHEN status = 'dispatch_failed'
                                THEN 'dispatch_failed' ELSE 'failed' END,
                           CASE WHEN status = 'dispatch_failed'
                                THEN 'dispatch_failed'
                                ELSE 'legacy_unlinked_request' END,
                           created_at, updated_at, ?
                    FROM management_run_requests_legacy
                    """,
                    (now,),
                )
            self.connection.execute(
                "DROP TABLE management_run_requests_legacy"
            )
            self.connection.execute(
                """
                CREATE INDEX idx_management_run_requests_created
                ON management_run_requests(created_at DESC)
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX idx_management_run_requests_active
                ON management_run_requests((1))
                WHERE status IN ('queued', 'running')
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX idx_management_run_requests_probe_run
                ON management_run_requests(probe_run_id)
                WHERE probe_run_id IS NOT NULL
                """
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _migrate_strategy_dependency_name(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(strategy_dependencies)"
            ).fetchall()
        }
        if "strategy_name" in columns:
            return
        try:
            with self.connection:
                self.connection.execute(
                    """
                    ALTER TABLE strategy_dependencies
                    ADD COLUMN strategy_name TEXT NOT NULL DEFAULT ''
                    """
                )
        except sqlite3.OperationalError:
            refreshed = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(strategy_dependencies)"
                ).fetchall()
            }
            if "strategy_name" not in refreshed:
                raise

    def _migrate_element_catalog_schema(self) -> None:
        managed_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(managed_elements)"
            ).fetchall()
        }
        additions = {
            "scope": "TEXT NOT NULL DEFAULT 'page'",
            "primary_locator_type": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name in managed_columns:
                continue
            try:
                with self.connection:
                    self.connection.execute(
                        f"ALTER TABLE managed_elements ADD COLUMN {name} {definition}"
                    )
            except sqlite3.OperationalError:
                refreshed = {
                    row["name"]
                    for row in self.connection.execute(
                        "PRAGMA table_info(managed_elements)"
                    ).fetchall()
                }
                if name not in refreshed:
                    raise

        draft_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(element_drafts)"
            ).fetchall()
        }
        foreign_tables = {
            row["table"]
            for row in self.connection.execute(
                "PRAGMA foreign_key_list(element_drafts)"
            ).fetchall()
        }
        if (
            "created_by_username" in draft_columns
            and foreign_tables == {"managed_elements"}
        ):
            return

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            draft_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(element_drafts)"
                ).fetchall()
            }
            foreign_tables = {
                row["table"]
                for row in self.connection.execute(
                    "PRAGMA foreign_key_list(element_drafts)"
                ).fetchall()
            }
            if (
                "created_by_username" in draft_columns
                and foreign_tables == {"managed_elements"}
            ):
                self.connection.commit()
                return
            self.connection.execute(
                "ALTER TABLE element_drafts RENAME TO element_drafts_legacy"
            )
            self.connection.execute(
                """
                CREATE TABLE element_drafts (
                    element_id TEXT PRIMARY KEY
                        REFERENCES managed_elements(id) ON DELETE CASCADE,
                    contract_json TEXT NOT NULL,
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    base_version_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1
                        CHECK (revision >= 1),
                    created_by INTEGER NOT NULL CHECK (created_by > 0),
                    created_by_username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            username_expression = (
                "created_by_username"
                if "created_by_username" in draft_columns
                else "'unknown'"
            )
            self.connection.execute(
                f"""
                INSERT INTO element_drafts (
                    element_id,
                    contract_json,
                    candidates_json,
                    validation_json,
                    base_version_id,
                    revision,
                    created_by,
                    created_by_username,
                    created_at,
                    updated_at
                )
                SELECT element_id,
                       contract_json,
                       candidates_json,
                       validation_json,
                       base_version_id,
                       revision,
                       created_by,
                       {username_expression},
                       created_at,
                       updated_at
                FROM element_drafts_legacy
                """
            )
            self.connection.execute("DROP TABLE element_drafts_legacy")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _migrate_element_publication_workflow(self) -> None:
        for table, additions in (
            (
                "selector_versions",
                {
                    "element_request_id": "TEXT NOT NULL DEFAULT ''",
                    "element_request_generation": (
                        "INTEGER NOT NULL DEFAULT 0"
                    ),
                },
            ),
            (
                "publication_outbox",
                {
                    "element_request_id": "TEXT NOT NULL DEFAULT ''",
                    "element_request_generation": (
                        "INTEGER NOT NULL DEFAULT 0"
                    ),
                },
            ),
        ):
            columns = {
                row["name"]
                for row in self.connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for name, definition in additions.items():
                if name not in columns:
                    with self.connection:
                        self.connection.execute(
                            f"ALTER TABLE {table} "
                            f"ADD COLUMN {name} {definition}"
                        )

        request_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(element_request_outbox)"
            ).fetchall()
        }
        request_sql_row = self.connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'element_request_outbox'
            """
        ).fetchone()
        request_sql = str(request_sql_row["sql"] or "")
        needs_rebuild = (
            "staged_version_id" not in request_columns
            or "staged_result_json" not in request_columns
            or "'publishing'" not in request_sql
        )
        if needs_rebuild:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    """
                    ALTER TABLE element_request_outbox
                    RENAME TO element_request_outbox_legacy
                    """
                )
                self.connection.execute(
                    """
                    CREATE TABLE element_request_outbox (
                        request_id TEXT PRIMARY KEY,
                        request_type TEXT NOT NULL CHECK (
                            request_type IN ('probe', 'validate')
                        ),
                        element_id TEXT NOT NULL,
                        expected_revision INTEGER NOT NULL
                            CHECK (expected_revision >= 1),
                        contract_json TEXT NOT NULL,
                        actor_user_id INTEGER NOT NULL
                            CHECK (actor_user_id > 0),
                        actor_username TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'pending', 'processing', 'publishing',
                                'completed', 'failed'
                            )
                        ),
                        attempt_count INTEGER NOT NULL DEFAULT 0
                            CHECK (attempt_count >= 0),
                        claim_token TEXT NOT NULL DEFAULT '',
                        claim_generation INTEGER NOT NULL DEFAULT 0
                            CHECK (claim_generation >= 0),
                        lease_until TEXT,
                        next_attempt_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        error_code TEXT NOT NULL DEFAULT '',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        staged_version_id TEXT NOT NULL DEFAULT '',
                        staged_result_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                staged_version = (
                    "staged_version_id"
                    if "staged_version_id" in request_columns
                    else "''"
                )
                staged_result = (
                    "staged_result_json"
                    if "staged_result_json" in request_columns
                    else "'{}'"
                )
                self.connection.execute(
                    f"""
                    INSERT INTO element_request_outbox (
                        request_id, request_type, element_id,
                        expected_revision, contract_json, actor_user_id,
                        actor_username, status, attempt_count, claim_token,
                        claim_generation, lease_until, next_attempt_at,
                        started_at, completed_at, error_code, result_json,
                        staged_version_id, staged_result_json,
                        created_at, updated_at
                    )
                    SELECT request_id, request_type, element_id,
                           expected_revision, contract_json, actor_user_id,
                           actor_username, status, attempt_count, claim_token,
                           claim_generation, lease_until, next_attempt_at,
                           started_at, completed_at, error_code, result_json,
                           {staged_version}, {staged_result},
                           created_at, updated_at
                    FROM element_request_outbox_legacy
                    """
                )
                self.connection.execute(
                    "DROP TABLE element_request_outbox_legacy"
                )
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
        with self.connection:
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_element_request_outbox_claim
                ON element_request_outbox(
                    status, next_attempt_at, lease_until, created_at
                )
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_selector_version_element_request
                ON selector_versions(element_request_id)
                WHERE element_request_id <> ''
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_publication_element_request
                ON publication_outbox(element_request_id)
                WHERE element_request_id <> ''
                """
            )

    def _migrate_contract_scope_key(self) -> None:
        columns = self.connection.execute(
            "PRAGMA table_info(element_probe_contracts)"
        ).fetchall()
        primary = {
            row["name"]: int(row["pk"])
            for row in columns
            if int(row["pk"]) > 0
        }
        if primary == {"site": 1, "environment": 2, "alias": 3}:
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                ALTER TABLE element_probe_contracts
                RENAME TO element_probe_contracts_legacy
                """
            )
            self.connection.execute(
                """
                CREATE TABLE element_probe_contracts (
                    alias TEXT NOT NULL,
                    site TEXT NOT NULL DEFAULT 'tiktok',
                    environment TEXT NOT NULL DEFAULT 'production',
                    contract_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(site, environment, alias)
                )
                """
            )
            self.connection.execute(
                """
                INSERT INTO element_probe_contracts (
                    alias,
                    site,
                    environment,
                    contract_json,
                    enabled,
                    updated_at
                )
                SELECT alias,
                       COALESCE(NULLIF(site, ''), 'tiktok'),
                       COALESCE(NULLIF(environment, ''), 'production'),
                       contract_json,
                       enabled,
                       updated_at
                FROM element_probe_contracts_legacy
                """
            )
            self.connection.execute(
                "DROP TABLE element_probe_contracts_legacy"
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _migrate_alert_context(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(probe_alerts)"
            ).fetchall()
        }
        for name in ("site", "environment"):
            if name in columns:
                continue
            with self.connection:
                self.connection.execute(
                    f"""
                    ALTER TABLE probe_alerts
                    ADD COLUMN {name} TEXT NOT NULL DEFAULT 'unknown'
                    """
                )
        if "revision" not in columns:
            with self.connection:
                self.connection.execute(
                    """
                    ALTER TABLE probe_alerts
                    ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
                    """
                )
        with self.connection:
            self.connection.execute(
                """
                UPDATE probe_alerts
                SET site = 'unknown'
                WHERE site = ''
                """
            )
            self.connection.execute(
                """
                UPDATE probe_alerts
                SET environment = 'unknown'
                WHERE environment = ''
                """
            )

    def _migrate_management_idempotency(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(management_idempotency_cache)"
            ).fetchall()
        }
        additions = {
            "payload_hash": "TEXT NOT NULL DEFAULT ''",
            "state": "TEXT NOT NULL DEFAULT 'completed'",
            "request_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        with self.connection:
            for name, definition in additions.items():
                if name not in columns:
                    self.connection.execute(
                        f"ALTER TABLE management_idempotency_cache "
                        f"ADD COLUMN {name} {definition}"
                    )

    def _migrate_gate_reason_scope(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(strategy_gate_reasons)"
            ).fetchall()
        }
        with self.connection:
            for name in ("site", "environment"):
                if name not in columns:
                    self.connection.execute(
                        f"""
                        ALTER TABLE strategy_gate_reasons
                        ADD COLUMN {name} TEXT NOT NULL DEFAULT ''
                        """
                    )
            self.connection.execute(
                """
                UPDATE strategy_gate_reasons
                SET site = 'tiktok', environment = 'production'
                WHERE source = 'probe'
                  AND (site = '' OR environment = '')
                """
            )
            self.connection.execute("DROP INDEX IF EXISTS idx_open_gate_reason")
            self.connection.execute(
                """
                CREATE UNIQUE INDEX idx_open_gate_reason
                ON strategy_gate_reasons(
                    strategy_id,
                    source,
                    site,
                    environment,
                    reason_code,
                    selector_version_id
                )
                WHERE cleared_at IS NULL
                """
            )

    def _migrate_probe_effect_scope(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(probe_effect_outbox)"
            ).fetchall()
        }
        table_row = self.connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'probe_effect_outbox'
            """
        ).fetchone()
        table_sql = str(table_row["sql"]) if table_row is not None else ""
        if (
            {"site", "environment"} <= columns
            and "probe_unavailable" in table_sql
        ):
            with self.connection:
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_probe_effect_scope_pending
                    ON probe_effect_outbox(site, environment, status, id)
                    """
                )
            return
        rows = self.connection.execute(
            "SELECT * FROM probe_effect_outbox ORDER BY id"
        ).fetchall()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                ALTER TABLE probe_effect_outbox
                RENAME TO probe_effect_outbox_legacy
                """
            )
            self.connection.execute(
                """
                CREATE TABLE probe_effect_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_key TEXT NOT NULL UNIQUE,
                    site TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK (
                        event_type IN (
                            'selector_failure',
                            'probe_unavailable',
                            'probe_stale',
                            'recovery'
                        )
                    ),
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'store_applied', 'completed')
                    ),
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            for row in rows:
                payload = _decode_json_object(
                    row["payload_json"],
                    "probe effect payload",
                )
                site = payload.get("site", "tiktok")
                environment = payload.get("environment", "production")
                try:
                    site_value = _key_segment(site, "site")
                    environment_value = _key_segment(
                        environment,
                        "environment",
                    )
                except ValueError:
                    site_value = "tiktok"
                    environment_value = "production"
                self.connection.execute(
                    """
                    INSERT INTO probe_effect_outbox (
                        id, effect_key, site, environment, event_type,
                        payload_json, result_json, status, created_at,
                        completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["effect_key"],
                        site_value,
                        environment_value,
                        row["event_type"],
                        row["payload_json"],
                        row["result_json"],
                        row["status"],
                        row["created_at"],
                        row["completed_at"],
                    ),
                )
            self.connection.execute(
                "DROP TABLE probe_effect_outbox_legacy"
            )
            self.connection.execute(
                """
                CREATE INDEX idx_probe_effect_outbox_pending
                ON probe_effect_outbox(status, id)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX idx_probe_effect_scope_pending
                ON probe_effect_outbox(site, environment, status, id)
                """
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def open_or_update_alert(
        self,
        *,
        fingerprint: str,
        failure_class: str,
        aliases: Sequence[str],
        strategy_ids: Sequence[str],
        active_version: str,
        details: Mapping[str, object],
        site: str,
        environment: str,
        now: str,
    ) -> dict[str, object]:
        fingerprint_value = _required_text(fingerprint, "fingerprint")
        failure = _required_text(failure_class, "failure_class")
        site_value = _required_text(site, "site")
        environment_value = _required_text(environment, "environment")
        version = _required_text_or_empty(active_version, "active_version")
        timestamp = _iso_timestamp(now, "now")
        aliases_json = _json(sorted(set(aliases)))
        strategies_json = _json(sorted(set(strategy_ids)))
        details_json = _json(dict(details))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT id
                FROM probe_alerts
                WHERE fingerprint = ?
                  AND status IN ('open', 'acknowledged')
                """,
                (fingerprint_value,),
            ).fetchone()
            if row is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO probe_alerts (
                        fingerprint,
                        site,
                        environment,
                        status,
                        failure_class,
                        aliases_json,
                        strategy_ids_json,
                        active_version,
                        first_seen_at,
                        last_seen_at,
                        occurrence_count,
                        details_json
                    ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        fingerprint_value,
                        site_value,
                        environment_value,
                        failure,
                        aliases_json,
                        strategies_json,
                        version,
                        timestamp,
                        timestamp,
                        details_json,
                    ),
                )
                alert_id = int(cursor.lastrowid)
                event_type = "alert_opened"
            else:
                alert_id = int(row["id"])
                self.connection.execute(
                    """
                    UPDATE probe_alerts
                    SET last_seen_at = ?,
                        occurrence_count = occurrence_count + 1,
                        revision = revision + 1,
                        details_json = ?,
                        aliases_json = ?,
                        strategy_ids_json = ?
                    WHERE id = ?
                    """,
                    (
                        timestamp,
                        details_json,
                        aliases_json,
                        strategies_json,
                        alert_id,
                    ),
                )
                event_type = "alert_updated"
            alert = self.connection.execute(
                "SELECT * FROM probe_alerts WHERE id = ?",
                (alert_id,),
            ).fetchone()
            if event_type == "alert_opened":
                payload_json = _alert_webhook_payload(
                    alert,
                    event_type=event_type,
                )
                self.connection.execute(
                    """
                    INSERT INTO webhook_outbox (
                        alert_id,
                        event_type,
                        payload_json,
                        status,
                        next_attempt_at,
                        created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        alert_id,
                        event_type,
                        payload_json,
                        timestamp,
                        timestamp,
                    ),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return _alert_record(alert)

    def transition_alert(
        self,
        alert_id: int,
        *,
        status: str,
        now: str,
    ) -> dict[str, object]:
        selected_id = _positive_integer(alert_id, "alert_id")
        if status not in {"acknowledged", "resolved"}:
            raise ValueError("unsupported alert status")
        timestamp = _iso_timestamp(now, "now")
        timestamp_column = (
            "acknowledged_at" if status == "acknowledged" else "resolved_at"
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT * FROM probe_alerts WHERE id = ?",
                (selected_id,),
            ).fetchone()
            if current is None:
                raise KeyError("alert not found")
            if current["status"] == "resolved":
                if status != "resolved":
                    raise ValueError("resolved alert cannot be acknowledged")
                self.connection.commit()
                return _alert_record(current)
            if current["status"] != status:
                self.connection.execute(
                    f"""
                    UPDATE probe_alerts
                    SET status = ?, {timestamp_column} = ?,
                        revision = revision + 1
                    WHERE id = ?
                    """,
                    (status, timestamp, selected_id),
                )
                current = self.connection.execute(
                    "SELECT * FROM probe_alerts WHERE id = ?",
                    (selected_id,),
                ).fetchone()
                event_type = (
                    "alert_acknowledged"
                    if status == "acknowledged"
                    else "alert_resolved"
                )
                self.connection.execute(
                    """
                    INSERT INTO webhook_outbox (
                        alert_id,
                        event_type,
                        payload_json,
                        status,
                        next_attempt_at,
                        created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        selected_id,
                        event_type,
                        _alert_webhook_payload(
                            current,
                            event_type=event_type,
                        ),
                        timestamp,
                        timestamp,
                    ),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return _alert_record(current)

    def record_alert_screenshot(
        self,
        *,
        alert_id: int,
        path: str,
        created_at: str,
    ) -> None:
        selected_id = _positive_integer(alert_id, "alert_id")
        selected_path = _required_text(path, "path")
        timestamp = _iso_timestamp(created_at, "created_at")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE probe_alerts
                SET screenshot_path = ?
                WHERE id = ?
                """,
                (selected_path, selected_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("alert not found")
            self.connection.execute(
                """
                INSERT INTO probe_alert_screenshots (
                    alert_id,
                    path,
                    created_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(alert_id) DO UPDATE SET
                    path = excluded.path,
                    created_at = excluded.created_at
                """,
                (selected_id, selected_path, timestamp),
            )

    def expired_alert_screenshots(
        self,
        *,
        before: str,
    ) -> list[dict[str, object]]:
        cutoff = _iso_timestamp(before, "before")
        rows = self.connection.execute(
            """
            SELECT alert_id, path, created_at
            FROM probe_alert_screenshots
            WHERE created_at < ?
            ORDER BY alert_id
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]

    def forget_alert_screenshot(
        self,
        *,
        alert_id: int,
        path: str,
    ) -> bool:
        selected_id = _positive_integer(alert_id, "alert_id")
        selected_path = _required_text(path, "path")
        with self.connection:
            cursor = self.connection.execute(
                """
                DELETE FROM probe_alert_screenshots
                WHERE alert_id = ? AND path = ?
                """,
                (selected_id, selected_path),
            )
            if cursor.rowcount:
                self.connection.execute(
                    """
                    UPDATE probe_alerts
                    SET screenshot_path = ''
                    WHERE id = ? AND screenshot_path = ?
                    """,
                    (selected_id, selected_path),
                )
            return cursor.rowcount == 1

    def claim_webhook_delivery(
        self,
        *,
        now: str,
        lease_seconds: int = 60,
    ) -> dict[str, object] | None:
        timestamp = _iso_timestamp(now, "now")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
            or lease_seconds > 600
        ):
            raise ValueError("lease_seconds must be between 1 and 600")
        lease_until = (
            datetime.fromisoformat(timestamp) + timedelta(seconds=lease_seconds)
        ).isoformat()
        claim_token = uuid.uuid4().hex
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE webhook_outbox
                SET status = 'failed',
                    completed_at = ?,
                    lease_until = NULL,
                    last_error = CASE
                        WHEN last_error = '' THEN 'attempt_limit_exceeded'
                        ELSE last_error
                    END
                WHERE status IN ('pending', 'processing')
                  AND attempt_count >= 5
                """,
                (timestamp,),
            )
            row = self.connection.execute(
                """
                SELECT id
                FROM webhook_outbox
                WHERE (
                    status = 'pending'
                    AND attempt_count < 5
                    AND next_attempt_at <= ?
                ) OR (
                    status = 'processing'
                    AND attempt_count < 5
                    AND lease_until IS NOT NULL
                    AND lease_until <= ?
                )
                ORDER BY id
                LIMIT 1
                """,
                (timestamp, timestamp),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            outbox_id = int(row["id"])
            self.connection.execute(
                """
                UPDATE webhook_outbox
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    claim_token = ?,
                    claim_generation = claim_generation + 1,
                    lease_until = ?,
                    last_error = ''
                WHERE id = ?
                """,
                (claim_token, lease_until, outbox_id),
            )
            claimed = self.connection.execute(
                "SELECT * FROM webhook_outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        result = dict(claimed)
        result["payload"] = _decode_json_object(
            result.pop("payload_json"),
            "webhook payload",
        )
        return result

    def complete_webhook_delivery(
        self,
        *,
        outbox_id: int,
        claim_token: str,
        claim_generation: int,
        completed_at: str,
    ) -> bool:
        timestamp = _iso_timestamp(completed_at, "completed_at")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE webhook_outbox
                SET status = 'completed',
                    completed_at = ?,
                    lease_until = NULL
                WHERE id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (
                    timestamp,
                    _positive_integer(outbox_id, "outbox_id"),
                    _required_text(claim_token, "claim_token"),
                    _positive_integer(claim_generation, "claim_generation"),
                ),
            )
            return cursor.rowcount == 1

    def fail_webhook_delivery(
        self,
        *,
        outbox_id: int,
        claim_token: str,
        claim_generation: int,
        error_code: str,
        next_attempt_at: str | None,
        failed_at: str,
    ) -> bool:
        failed_timestamp = _iso_timestamp(failed_at, "failed_at")
        next_timestamp = (
            _iso_timestamp(next_attempt_at, "next_attempt_at")
            if next_attempt_at is not None
            else failed_timestamp
        )
        status = "pending" if next_attempt_at is not None else "failed"
        completed_at = None if next_attempt_at is not None else failed_timestamp
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE webhook_outbox
                SET status = ?,
                    next_attempt_at = ?,
                    completed_at = ?,
                    lease_until = NULL,
                    last_error = ?
                WHERE id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (
                    status,
                    next_timestamp,
                    completed_at,
                    _required_text(error_code, "error_code")[:128],
                    _positive_integer(outbox_id, "outbox_id"),
                    _required_text(claim_token, "claim_token"),
                    _positive_integer(claim_generation, "claim_generation"),
                ),
            )
            return cursor.rowcount == 1

    def _migrate_attempt_token(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(probe_runs)"
            ).fetchall()
        }
        if "attempt_token" in columns:
            return
        try:
            with self.connection:
                self.connection.execute(
                    """
                    ALTER TABLE probe_runs
                    ADD COLUMN attempt_token TEXT NOT NULL DEFAULT ''
                    """
                )
        except sqlite3.OperationalError:
            columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(probe_runs)"
                ).fetchall()
            }
            if "attempt_token" not in columns:
                raise

    def _migrate_outbox_claims(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(publication_outbox)"
            ).fetchall()
        }
        additions = {
            "claim_token": "TEXT NOT NULL DEFAULT ''",
            "claim_generation": "INTEGER NOT NULL DEFAULT 0",
            "lease_until": "TEXT",
            "last_error": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name in columns:
                continue
            try:
                with self.connection:
                    self.connection.execute(
                        f"ALTER TABLE publication_outbox "
                        f"ADD COLUMN {name} {definition}"
                    )
            except sqlite3.OperationalError:
                refreshed = {
                    row["name"]
                    for row in self.connection.execute(
                        "PRAGMA table_info(publication_outbox)"
                    ).fetchall()
                }
                if name not in refreshed:
                    raise
        with self.connection:
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_publication_outbox_due
                ON publication_outbox(status, next_attempt_at, lease_until)
                """
            )

    def _migrate_publication_fence(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(selector_versions)"
            ).fetchall()
        }
        additions = {
            "probe_run_id": "INTEGER REFERENCES probe_runs(id)",
            "lease_owner": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name in columns:
                continue
            try:
                with self.connection:
                    self.connection.execute(
                        f"ALTER TABLE selector_versions "
                        f"ADD COLUMN {name} {definition}"
                    )
            except sqlite3.OperationalError:
                refreshed = {
                    row["name"]
                    for row in self.connection.execute(
                        "PRAGMA table_info(selector_versions)"
                    ).fetchall()
                }
                if name not in refreshed:
                    raise

    def start_run(
        self,
        *,
        scheduled_for: str,
        active_version_before: str,
        attempt_token: str = "",
        management_request_id: str = "",
        trigger: str = "scheduled",
    ) -> int:
        slot, _ = _scheduled_slot(scheduled_for)
        active_version = _required_text_or_empty(
            active_version_before,
            "active_version_before",
        )
        token = _required_text_or_empty(
            attempt_token,
            "attempt_token",
        )
        request_id = _required_text_or_empty(
            management_request_id,
            "management_request_id",
        )
        trigger_text = _required_text(trigger, "trigger")
        if trigger_text not in {"manual", "scheduled", "retry"}:
            raise ValueError("trigger is invalid")
        started_at = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT id, status
                FROM probe_runs
                WHERE scheduled_for = ?
                """,
                (slot,),
            ).fetchone()
            if row is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO probe_runs (
                        scheduled_for,
                        started_at,
                        status,
                        attempt_token,
                        active_version_before
                    ) VALUES (?, ?, 'running', ?, ?)
                    """,
                    (slot, started_at, token, active_version),
                )
                run_id = int(cursor.lastrowid)
            else:
                run_id = int(row["id"])
                if row["status"] == "completed":
                    raise RuntimeError(
                        "scheduled selector probe run is already completed"
                    )
                self.connection.execute(
                    """
                    DELETE FROM selector_validation_runs
                    WHERE probe_run_id = ?
                    """,
                    (run_id,),
                )
                self.connection.execute(
                    """
                    UPDATE probe_runs
                    SET started_at = ?,
                        finished_at = NULL,
                        status = 'running',
                        attempt_token = ?,
                        active_version_before = ?,
                        published_version_after = '',
                        failed_aliases_json = '[]',
                        details_json = '{}'
                    WHERE id = ?
                    """,
                    (started_at, token, active_version, run_id),
                )
            if request_id:
                cursor = self.connection.execute(
                    """
                    UPDATE management_run_requests
                    SET probe_run_id = ?, status = 'running',
                        updated_at = ?, finished_at = NULL,
                        failure_code = ''
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (run_id, started_at, request_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "management run request is not active"
                    )
            self.connection.commit()
            return run_id
        except BaseException:
            self.connection.rollback()
            raise

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        details: Mapping[str, object],
        published_version_after: str = "",
        failed_aliases: Sequence[str] = (),
        attempt_token: str = "",
        policy: Mapping[str, object] | None = None,
        effect: Mapping[str, object] | None = None,
    ) -> None:
        run_id = _positive_integer(run_id, "run_id")
        status_text = _required_text(status, "status")
        if not isinstance(details, Mapping):
            raise ValueError("details must be a JSON object")
        published_version = _required_text_or_empty(
            published_version_after,
            "published_version_after",
        )
        aliases = _string_sequence(failed_aliases, "failed_aliases")
        token = _required_text_or_empty(attempt_token, "attempt_token")
        details_json = _json(dict(details))
        aliases_json = _json(aliases)
        prepared_policy = _prepare_probe_policy(policy)
        prepared_effect = _prepare_probe_effect(effect)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            finished_at = _utc_now()
            cursor = self.connection.execute(
                """
                UPDATE probe_runs
                SET finished_at = ?,
                    status = ?,
                    published_version_after = ?,
                    failed_aliases_json = ?,
                    details_json = ?
                WHERE id = ? AND attempt_token = ?
                """,
                (
                    finished_at,
                    status_text,
                    published_version,
                    aliases_json,
                    details_json,
                    run_id,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale probe attempt cannot finish run")
            terminal_status = (
                "completed" if status_text == "completed" else "failed"
            )
            failure_code = ""
            if terminal_status == "failed":
                raw_failure_code = details.get("failure_code", status_text)
                if isinstance(raw_failure_code, str):
                    failure_code = raw_failure_code.strip()[:128]
                failure_code = failure_code or "probe_failed"
            self.connection.execute(
                """
                UPDATE management_run_requests
                SET status = ?, failure_code = ?, updated_at = ?,
                    finished_at = ?
                WHERE probe_run_id = ?
                  AND status IN ('queued', 'running')
                """,
                (
                    terminal_status,
                    failure_code,
                    finished_at,
                    finished_at,
                    run_id,
                ),
            )
            if prepared_policy is not None:
                _update_probe_health(
                    self.connection,
                    *prepared_policy,
                )
            if prepared_effect is not None:
                (
                    effect_key,
                    event_type,
                    effect_site,
                    effect_environment,
                    payload_json,
                ) = prepared_effect
                self.connection.execute(
                    """
                    INSERT INTO probe_effect_outbox (
                        effect_key,
                        site,
                        environment,
                        event_type,
                        payload_json,
                        status,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    ON CONFLICT(effect_key) DO NOTHING
                    """,
                    (
                        effect_key,
                        effect_site,
                        effect_environment,
                        event_type,
                        payload_json,
                        _utc_now(),
                    ),
                )
                existing = self.connection.execute(
                    """
                    SELECT site, environment, event_type, payload_json
                    FROM probe_effect_outbox
                    WHERE effect_key = ?
                    """,
                    (effect_key,),
                ).fetchone()
                if (
                    existing is None
                    or existing["site"] != effect_site
                    or existing["environment"] != effect_environment
                    or existing["event_type"] != event_type
                    or existing["payload_json"] != payload_json
                ):
                    raise RuntimeError("probe effect key collision")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def probe_health_state(
        self,
        *,
        site: str = "tiktok",
        environment: str = "production",
    ) -> dict[str, object]:
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        row = self.connection.execute(
            """
            SELECT site, environment, failure_started_at, retry_count,
                   next_retry_at, last_validated_at
            FROM probe_health_state
            WHERE site = ? AND environment = ?
            """,
            (site_value, environment_value),
        ).fetchone()
        if row is None:
            return {
                "site": site_value,
                "environment": environment_value,
                "failure_started_at": "",
                "retry_count": 0,
                "next_retry_at": "",
                "last_validated_at": "",
            }
        return dict(row)

    def pending_probe_effects(
        self,
        *,
        site: str = "tiktok",
        environment: str = "production",
        limit: int = 32,
    ) -> list[dict[str, object]]:
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 256
        ):
            raise ValueError("limit must be between 1 and 256")
        rows = self.connection.execute(
            """
            SELECT id, effect_key, event_type, payload_json, status,
                   result_json, created_at
            FROM probe_effect_outbox
            WHERE site = ?
              AND environment = ?
              AND status IN ('pending', 'store_applied')
            ORDER BY id
            LIMIT ?
            """,
            (site_value, environment_value, limit),
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _decode_json_object(
                item.pop("payload_json"),
                "probe effect payload",
            )
            item["result"] = _decode_json_object(
                item.pop("result_json"),
                "probe effect result",
            )
            result.append(item)
        return result

    def apply_probe_effect(
        self,
        effect_id: int,
        *,
        site: str = "tiktok",
        environment: str = "production",
    ) -> dict[str, object]:
        selected_id = _positive_integer(effect_id, "effect_id")
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT event_type, payload_json, result_json, status
                FROM probe_effect_outbox
                WHERE id = ? AND site = ? AND environment = ?
                """,
                (selected_id, site_value, environment_value),
            ).fetchone()
            if row is None:
                raise KeyError("probe effect not found")
            if row["status"] != "pending":
                result = _decode_json_object(
                    row["result_json"],
                    "probe effect result",
                )
                self.connection.commit()
                return result
            payload = _decode_json_object(
                row["payload_json"],
                "probe effect payload",
            )
            if row["event_type"] == "selector_failure":
                result = _apply_selector_failure_effect(
                    self.connection,
                    payload,
                )
            elif row["event_type"] == "probe_stale":
                result = _apply_probe_stale_effect(
                    self.connection,
                    payload,
                )
            elif row["event_type"] == "probe_unavailable":
                result = _apply_probe_unavailable_effect(
                    self.connection,
                    payload,
                )
            elif row["event_type"] == "recovery":
                result = _apply_recovery_effect(
                    self.connection,
                    payload,
                )
            else:
                raise RuntimeError("unsupported probe effect")
            result_json = _json(result)
            self.connection.execute(
                """
                UPDATE probe_effect_outbox
                SET status = 'store_applied', result_json = ?
                WHERE id = ?
                  AND site = ?
                  AND environment = ?
                  AND status = 'pending'
                """,
                (
                    result_json,
                    selected_id,
                    site_value,
                    environment_value,
                ),
            )
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def complete_probe_effect(
        self,
        effect_id: int,
        *,
        site: str = "tiktok",
        environment: str = "production",
    ) -> bool:
        selected_id = _positive_integer(effect_id, "effect_id")
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE probe_effect_outbox
                SET status = 'completed', completed_at = ?
                WHERE id = ?
                  AND site = ?
                  AND environment = ?
                  AND status = 'store_applied'
                """,
                (
                    _utc_now(),
                    selected_id,
                    site_value,
                    environment_value,
                ),
            )
        return cursor.rowcount == 1

    def recovery_pending(
        self,
        *,
        site: str,
        environment: str,
    ) -> bool:
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        gate = self.connection.execute(
            """
            SELECT 1
            FROM strategy_gate_reasons
            WHERE source = 'probe'
              AND site = ?
              AND environment = ?
              AND cleared_at IS NULL
            LIMIT 1
            """,
            (site_value, environment_value),
        ).fetchone()
        alert = self.connection.execute(
            """
            SELECT 1
            FROM probe_alerts
            WHERE site = ?
              AND environment = ?
              AND status IN ('open', 'acknowledged')
              AND failure_class IN (
                  'selector_validation_failed',
                  'probe_unavailable'
              )
            LIMIT 1
            """,
            (site_value, environment_value),
        ).fetchone()
        return gate is not None or alert is not None

    def last_completed_slot(self) -> datetime | None:
        rows = self.connection.execute(
            """
            SELECT scheduled_for
            FROM probe_runs
            WHERE status = 'completed'
            """
        ).fetchall()
        slots = [_scheduled_slot(row["scheduled_for"])[1] for row in rows]
        return max(slots, default=None)

    def last_terminal_slot(self) -> datetime | None:
        rows = self.connection.execute(
            """
            SELECT scheduled_for
            FROM probe_runs
            WHERE status IN ('completed', 'selector_validation_failed')
            """
        ).fetchall()
        slots = [_scheduled_slot(row["scheduled_for"])[1] for row in rows]
        return max(slots, default=None)

    def probe_run_state(
        self,
        scheduled_for: str,
    ) -> dict[str, object] | None:
        slot, _ = _scheduled_slot(scheduled_for)
        row = self.connection.execute(
            """
            SELECT id, status, started_at, finished_at, details_json
            FROM probe_runs
            WHERE scheduled_for = ?
            """,
            (slot,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = _decode_json_object(
            result.pop("details_json"),
            "probe run details",
        )
        return result

    def save_contracts(self, contracts: object) -> None:
        prepared = _prepare_contracts(contracts)
        updated_at = _utc_now()
        scopes = sorted(
            {(site, environment) for _, site, environment, _, _ in prepared}
        )
        with self.connection:
            for site, environment in scopes:
                self.connection.execute(
                    """
                    DELETE FROM element_probe_contracts
                    WHERE site = ? AND environment = ?
                    """,
                    (site, environment),
                )
            self.connection.executemany(
                """
                INSERT INTO element_probe_contracts (
                    alias,
                    site,
                    environment,
                    contract_json,
                    enabled,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        alias,
                        site,
                        environment,
                        contract_json,
                        int(enabled),
                        updated_at,
                    )
                    for (
                        alias,
                        site,
                        environment,
                        contract_json,
                        enabled,
                    ) in prepared
                ],
            )

    def seed_contracts(self, contracts: object) -> int:
        prepared = _prepare_contracts(contracts)
        updated_at = _utc_now()
        inserted = 0
        with self.connection:
            for alias, site, environment, contract_json, enabled in prepared:
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO element_probe_contracts (
                        alias,
                        site,
                        environment,
                        contract_json,
                        enabled,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alias,
                        site,
                        environment,
                        contract_json,
                        int(enabled),
                        updated_at,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def upsert_contract(
        self,
        alias: str,
        contract: Mapping[str, object],
        *,
        site: str = "tiktok",
        environment: str = "production",
        enabled: bool = True,
    ) -> None:
        if not isinstance(contract, Mapping):
            raise ValueError("contract must be a JSON object")
        prepared = _prepare_contracts(
            {
                alias: {
                    **dict(contract),
                    "site": site,
                    "environment": environment,
                    "enabled": enabled,
                }
            }
        )[0]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO element_probe_contracts (
                    alias,
                    site,
                    environment,
                    contract_json,
                    enabled,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(site, environment, alias) DO UPDATE SET
                    contract_json = excluded.contract_json,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (*prepared, _utc_now()),
            )

    def list_contracts(
        self,
        *,
        site: str = "tiktok",
        environment: str = "production",
        enabled_only: bool = True,
    ) -> dict[str, dict[str, object]]:
        site_value = _required_text(site, "site")
        environment_value = _required_text(environment, "environment")
        if not isinstance(enabled_only, bool):
            raise ValueError("enabled_only must be a boolean")
        rows = self.connection.execute(
            """
            SELECT alias, contract_json, enabled
            FROM element_probe_contracts
            WHERE site = ? AND environment = ?
              AND (? = 0 OR enabled = 1)
            ORDER BY alias
            """,
            (site_value, environment_value, int(enabled_only)),
        ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            contract = _decode_json_object(
                row["contract_json"],
                "contract",
            )
            for key in ("site", "environment", "enabled"):
                contract.pop(key, None)
            if not enabled_only:
                contract["enabled"] = bool(row["enabled"])
            result[str(row["alias"])] = contract
        return result

    def record_validation(
        self,
        *,
        run_id: int,
        profile_mask: str,
        round_number: int,
        page_state: str,
        result: str,
        failure_code: str,
        evidence: Mapping[str, object],
        screenshot_path: str = "",
        attempt_token: str = "",
    ) -> int:
        run_id = _positive_integer(run_id, "run_id")
        profile = _required_text(profile_mask, "profile_mask")
        if isinstance(round_number, bool) or round_number not in (1, 2):
            raise ValueError("round_number must be 1 or 2")
        state = _required_text(page_state, "page_state")
        result_text = _required_text(result, "result")
        failure = _required_text_or_empty(failure_code, "failure_code")
        screenshot = _required_text_or_empty(screenshot_path, "screenshot_path")
        if not isinstance(evidence, Mapping):
            raise ValueError("evidence must be a JSON object")
        token = _required_text_or_empty(attempt_token, "attempt_token")
        evidence_json = _json(dict(evidence))
        timestamp = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO selector_validation_runs (
                    probe_run_id,
                    profile_mask,
                    round_number,
                    page_state,
                    result,
                    failure_code,
                    evidence_json,
                    screenshot_path,
                    started_at,
                    finished_at
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                FROM probe_runs
                WHERE id = ? AND attempt_token = ?
                """,
                (
                    run_id,
                    profile,
                    round_number,
                    state,
                    result_text,
                    failure,
                    evidence_json,
                    screenshot,
                    timestamp,
                    timestamp,
                    run_id,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "stale probe attempt cannot record validation"
                )
            return int(cursor.lastrowid)

    def store_validated_version(
        self,
        *,
        bundle: object,
        evidence: object,
        base_version_id: str,
        model_id: str,
        prompt_version: str,
        site: str = "tiktok",
        environment: str = "production",
        probe_run_id: int | None = None,
        attempt_token: str = "",
        element_request_id: str = "",
        element_request_claim_token: str = "",
        element_request_generation: int = 0,
        staged_result: Mapping[str, object] | None = None,
    ) -> str:
        canonical_bundle, bundle_hash = _validated_bundle(bundle)
        evidence_value = _validated_evidence(
            evidence,
            bundle_hash,
            canonical_bundle["elements"],
        )
        base_version = _required_text_or_empty(
            base_version_id,
            "base_version_id",
        )
        model = _required_text_or_empty(model_id, "model_id")
        prompt = _required_text_or_empty(prompt_version, "prompt_version")
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        token = _required_text_or_empty(attempt_token, "attempt_token")
        if (probe_run_id is None) != (token == ""):
            raise ValueError(
                "probe_run_id and attempt_token must be supplied together"
            )
        run_id = (
            None
            if probe_run_id is None
            else _positive_integer(probe_run_id, "probe_run_id")
        )
        request_id = _required_text_or_empty(
            element_request_id,
            "element_request_id",
        )
        request_token = _required_text_or_empty(
            element_request_claim_token,
            "element_request_claim_token",
        )
        if bool(request_id) != bool(request_token):
            raise ValueError(
                "element request ID and claim token must be supplied together"
            )
        if request_id:
            request_generation = _positive_integer(
                element_request_generation,
                "element_request_generation",
            )
            if run_id is not None or not isinstance(staged_result, Mapping):
                raise ValueError("element request publication is invalid")
            _json(dict(staged_result))
        else:
            if element_request_generation not in (0, None):
                raise ValueError("element_request_generation is invalid")
            if staged_result is not None:
                raise ValueError("staged_result requires an element request")
            request_generation = 0
        now = datetime.now(UTC)
        digest = bundle_hash.removeprefix("sha256:")[:12]
        base_id = f"sel-{now.strftime('%Y%m%d-%H%M%S')}-{digest}"
        evidence_json = _canonical_json(evidence_value)
        timestamp = now.isoformat()

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if run_id is not None:
                owned_run = self.connection.execute(
                    """
                    SELECT 1
                    FROM probe_runs
                    WHERE id = ?
                      AND attempt_token = ?
                      AND status = 'running'
                    """,
                    (run_id, token),
                ).fetchone()
                if owned_run is None:
                    raise RuntimeError(
                        "stale probe attempt cannot store selector version"
                    )
            request_row = None
            if request_id:
                request_row = self.connection.execute(
                    """
                    SELECT request.*,
                           managed.revision AS managed_revision,
                           draft.contract_json AS current_contract_json
                    FROM element_request_outbox request
                    LEFT JOIN managed_elements managed
                        ON managed.id = request.element_id
                    LEFT JOIN element_drafts draft
                        ON draft.element_id = request.element_id
                    WHERE request.request_id = ?
                      AND request.request_type = 'validate'
                      AND request.status = 'processing'
                      AND request.claim_token = ?
                      AND request.claim_generation = ?
                      AND request.lease_until IS NOT NULL
                      AND request.lease_until > ?
                    """,
                    (
                        request_id,
                        request_token,
                        request_generation,
                        timestamp,
                    ),
                ).fetchone()
                if (
                    request_row is None
                    or request_row["managed_revision"] is None
                    or int(request_row["managed_revision"])
                    != int(request_row["expected_revision"])
                    or request_row["current_contract_json"]
                    != request_row["contract_json"]
                ):
                    raise RuntimeError(
                        "stale element request cannot store selector version"
                    )
            collisions = self.connection.execute(
                """
                SELECT *
                FROM selector_versions
                WHERE id = ? OR id LIKE ?
                ORDER BY id
                """,
                (base_id, base_id + "-%"),
            ).fetchall()
            for collision in collisions:
                collision_bundle = {
                    "version": collision["id"],
                    "bundle_hash": bundle_hash,
                    "elements": canonical_bundle["elements"],
                }
                collision_bundle_json = _canonical_json(collision_bundle)
                if not request_id and _same_version_content(
                    collision,
                    site=site_value,
                    environment=environment_value,
                    probe_run_id=run_id,
                    lease_owner=token,
                    base_version_id=base_version,
                    bundle_json=collision_bundle_json,
                    bundle_hash=bundle_hash,
                    evidence_json=evidence_json,
                    model_id=model,
                    prompt_version=prompt,
                ):
                    _ensure_single_outbox(
                        self.connection,
                        version_id=collision["id"],
                        payload_json=_publication_payload_json(
                            collision["id"],
                            base_version,
                            collision_bundle,
                            lease_owner=token,
                        ),
                        timestamp=timestamp,
                    )
                    self.connection.commit()
                    return str(collision["id"])

            version_id = base_id
            if collisions:
                for _attempt in range(16):
                    candidate = f"{base_id}-{secrets.token_hex(4)}"
                    exists = self.connection.execute(
                        "SELECT 1 FROM selector_versions WHERE id = ?",
                        (candidate,),
                    ).fetchone()
                    if exists is None:
                        version_id = candidate
                        break
                else:
                    raise RuntimeError("selector version ID allocation failed")
            version_bundle = {
                "version": version_id,
                "bundle_hash": bundle_hash,
                "elements": canonical_bundle["elements"],
            }
            bundle_json = _canonical_json(version_bundle)
            if len(bundle_json.encode("utf-8")) > 262_144:
                raise ValueError("bundle exceeds resource budget")
            payload_json = _publication_payload_json(
                version_id,
                base_version,
                version_bundle,
                lease_owner=token,
            )
            self.connection.execute(
                """
                INSERT INTO selector_versions (
                    id,
                    site,
                    environment,
                    probe_run_id,
                    lease_owner,
                    element_request_id,
                    element_request_generation,
                    status,
                    base_version_id,
                    bundle_json,
                    bundle_hash,
                    evidence_json,
                    model_id,
                    prompt_version,
                    created_at,
                    validated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'validated',
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    version_id,
                    site_value,
                    environment_value,
                    run_id,
                    token,
                    request_id,
                    request_generation,
                    base_version,
                    bundle_json,
                    bundle_hash,
                    evidence_json,
                    model,
                    prompt,
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO publication_outbox (
                    event_type,
                    aggregate_id,
                    element_request_id,
                    element_request_generation,
                    payload_json,
                    status,
                    next_attempt_at,
                    created_at
                ) VALUES (
                    'selector_version_validated', ?, ?, ?, ?,
                    'pending', ?, ?
                )
                """,
                (
                    version_id,
                    request_id,
                    request_generation,
                    payload_json,
                    timestamp,
                    timestamp,
                ),
            )
            if request_row is not None:
                safe_result = _safe_element_request_result(
                    staged_result or {},
                    str(request_row["element_id"]),
                )
                safe_result.update(
                    {
                        "status": "published",
                        "published": True,
                        "reconciled": True,
                        "new_version": version_id,
                    }
                )
                cursor = self.connection.execute(
                    """
                    UPDATE element_request_outbox
                    SET status = 'publishing',
                        staged_version_id = ?,
                        staged_result_json = ?,
                        updated_at = ?
                    WHERE request_id = ?
                      AND status = 'processing'
                      AND claim_token = ?
                      AND claim_generation = ?
                      AND lease_until > ?
                    """,
                    (
                        version_id,
                        _json(safe_result),
                        timestamp,
                        request_id,
                        request_token,
                        request_generation,
                        timestamp,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "stale element request cannot stage publication"
                    )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return version_id

    def claim_outbox_event(
        self,
        *,
        claim_token: str | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, object] | None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 0
            or lease_seconds > 3_600
        ):
            raise ValueError("lease_seconds must be between 0 and 3600")
        token = (
            _required_text(claim_token, "claim_token")
            if claim_token is not None
            else uuid.uuid4().hex
        )
        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT publication.id,
                       publication.status,
                       publication.next_attempt_at,
                       publication.lease_until
                FROM publication_outbox publication
                LEFT JOIN element_request_outbox request
                    ON request.request_id =
                        publication.element_request_id
                WHERE (
                    publication.element_request_id = ''
                    OR (
                        request.status = 'publishing'
                        AND request.request_type = 'validate'
                        AND request.staged_version_id =
                            publication.aggregate_id
                        AND request.claim_generation =
                            publication.element_request_generation
                    )
                )
                  AND publication.status IN ('pending', 'processing')
                ORDER BY publication.id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            if row["status"] == "pending" and row["next_attempt_at"] > now_text:
                self.connection.commit()
                return None
            if row["status"] == "processing" and (
                row["lease_until"] is None or row["lease_until"] > now_text
            ):
                self.connection.commit()
                return None
            outbox_id = int(row["id"])
            cursor = self.connection.execute(
                """
                UPDATE publication_outbox
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    claim_generation = claim_generation + 1,
                    claim_token = ?,
                    lease_until = ?,
                    last_error = ''
                WHERE id = ?
                  AND (
                    (status = 'pending' AND next_attempt_at <= ?)
                    OR (
                        status = 'processing'
                        AND lease_until IS NOT NULL
                        AND lease_until <= ?
                    )
                  )
                  AND (
                    element_request_id = ''
                    OR EXISTS (
                        SELECT 1
                        FROM element_request_outbox request
                        WHERE request.request_id =
                            publication_outbox.element_request_id
                          AND request.status = 'publishing'
                          AND request.request_type = 'validate'
                          AND request.staged_version_id =
                            publication_outbox.aggregate_id
                          AND request.claim_generation =
                            publication_outbox.element_request_generation
                    )
                  )
                """,
                (
                    token,
                    lease_until,
                    outbox_id,
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return None
            claimed = self.connection.execute(
                """
                SELECT id, aggregate_id, payload_json, attempt_count,
                       claim_token, claim_generation,
                       element_request_id, element_request_generation
                FROM publication_outbox
                WHERE id = ?
                """,
                (outbox_id,),
            ).fetchone()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        payload = _decode_json_object(claimed["payload_json"], "outbox payload")
        return {
            "outbox_id": int(claimed["id"]),
            "claim_token": claimed["claim_token"],
            "claim_generation": int(claimed["claim_generation"]),
            "attempt_count": int(claimed["attempt_count"]),
            **payload,
        }

    def next_outbox_event(
        self,
        *,
        claim_token: str | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, object] | None:
        return self.claim_outbox_event(
            claim_token=claim_token,
            lease_seconds=lease_seconds,
        )

    def ack_outbox_event(
        self,
        outbox_id: int,
        claim_token: str,
        claim_generation: int,
        *,
        outcome: str,
    ) -> bool:
        outbox_id = _positive_integer(outbox_id, "outbox_id")
        token = _required_text(claim_token, "claim_token")
        generation = _positive_integer(claim_generation, "claim_generation")
        if outcome not in {"published", "idempotent", "conflict"}:
            raise ValueError("unsupported publication outcome")
        completed_at = _utc_now()
        outbox_status = "completed" if outcome != "conflict" else "conflict"
        version_status = (
            "published" if outcome != "conflict" else "publication_failed"
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT publication.aggregate_id,
                       publication.element_request_id,
                       publication.element_request_generation,
                       request.request_type AS request_type,
                       request.element_id AS request_element_id,
                       request.actor_user_id AS request_actor_user_id,
                       request.actor_username AS request_actor_username,
                       request.attempt_count AS request_attempt_count,
                       request.claim_generation AS request_claim_generation,
                       request.status AS request_status,
                       request.staged_version_id AS request_staged_version_id,
                       request.staged_result_json AS request_staged_result_json
                FROM publication_outbox publication
                LEFT JOIN element_request_outbox request
                    ON request.request_id =
                        publication.element_request_id
                WHERE publication.id = ?
                  AND publication.status = 'processing'
                  AND publication.claim_token = ?
                  AND publication.claim_generation = ?
                """,
                (outbox_id, token, generation),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return False
            version_id = row["aggregate_id"]
            request_id = str(row["element_request_id"])
            if request_id and (
                row["request_status"] != "publishing"
                or row["request_type"] != "validate"
                or row["request_staged_version_id"] != version_id
                or int(row["element_request_generation"])
                != int(row["request_claim_generation"])
            ):
                self.connection.commit()
                return False
            if version_status == "published":
                version = self.connection.execute(
                    """
                    SELECT site, environment
                    FROM selector_versions
                    WHERE id = ?
                    """,
                    (version_id,),
                ).fetchone()
                if version is None:
                    raise RuntimeError("outbox selector version is missing")
                self.connection.execute(
                    """
                    UPDATE selector_versions
                    SET status = 'superseded'
                    WHERE site = ?
                      AND environment = ?
                      AND status = 'published'
                      AND id <> ?
                    """,
                    (version["site"], version["environment"], version_id),
                )
            self.connection.execute(
                """
                UPDATE selector_versions
                SET status = ?,
                    published_at = CASE
                        WHEN ? = 'published' THEN ?
                        ELSE published_at
                    END
                WHERE id = ?
                """,
                (
                    version_status,
                    version_status,
                    completed_at,
                    version_id,
                ),
            )
            cursor = self.connection.execute(
                """
                UPDATE publication_outbox
                SET status = ?,
                    completed_at = ?,
                    lease_until = NULL
                WHERE id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (
                    outbox_status,
                    completed_at,
                    outbox_id,
                    token,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return False
            if request_id and version_status == "published":
                _complete_staged_element_request(
                    self.connection,
                    row=row,
                    request_id=request_id,
                    version_id=str(version_id),
                    completed_at=completed_at,
                )
            elif request_id:
                _fail_staged_element_request(
                    self.connection,
                    request_id=request_id,
                    request_generation=int(
                        row["element_request_generation"]
                    ),
                    version_id=str(version_id),
                    error_code="publication_conflict",
                    completed_at=completed_at,
                )
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def cancel_outbox_event(
        self,
        outbox_id: int,
        claim_token: str,
        claim_generation: int,
    ) -> bool:
        outbox_id = _positive_integer(outbox_id, "outbox_id")
        token = _required_text(claim_token, "claim_token")
        generation = _positive_integer(claim_generation, "claim_generation")
        completed_at = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT aggregate_id,
                       element_request_id,
                       element_request_generation
                FROM publication_outbox
                WHERE id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (outbox_id, token, generation),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return False
            cursor = self.connection.execute(
                """
                UPDATE publication_outbox
                SET status = 'cancelled',
                    completed_at = ?,
                    lease_until = NULL,
                    last_error = 'lease_lost'
                WHERE id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (completed_at, outbox_id, token, generation),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return False
            self.connection.execute(
                """
                UPDATE selector_versions
                SET status = 'cancelled'
                WHERE id = ? AND status = 'validated'
                """,
                (row["aggregate_id"],),
            )
            if row["element_request_id"]:
                _fail_staged_element_request(
                    self.connection,
                    request_id=str(row["element_request_id"]),
                    request_generation=int(
                        row["element_request_generation"]
                    ),
                    version_id=str(row["aggregate_id"]),
                    error_code="publication_lease_lost",
                    completed_at=completed_at,
                )
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def cancel_validated_version(
        self,
        version_id: str,
        *,
        probe_run_id: int | None = None,
        attempt_token: str = "",
    ) -> bool:
        version = _required_text(version_id, "version_id")
        if (probe_run_id is None) != (attempt_token == ""):
            raise ValueError(
                "probe_run_id and attempt_token must be supplied together"
            )
        run_id = (
            None
            if probe_run_id is None
            else _positive_integer(probe_run_id, "probe_run_id")
        )
        token = (
            ""
            if run_id is None
            else _required_text(attempt_token, "attempt_token")
        )
        completed_at = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if run_id is None:
                cursor = self.connection.execute(
                    """
                    UPDATE selector_versions
                    SET status = 'cancelled'
                    WHERE id = ?
                      AND probe_run_id IS NULL
                      AND lease_owner = ''
                      AND element_request_id = ''
                      AND status = 'validated'
                    """,
                    (version,),
                )
            else:
                cursor = self.connection.execute(
                    """
                    UPDATE selector_versions
                    SET status = 'cancelled'
                    WHERE id = ?
                      AND probe_run_id = ?
                      AND lease_owner = ?
                      AND status = 'validated'
                    """,
                    (version, run_id, token),
                )
            if cursor.rowcount != 1:
                self.connection.commit()
                return False
            self.connection.execute(
                """
                UPDATE publication_outbox
                SET status = 'cancelled',
                    completed_at = ?,
                    lease_until = NULL,
                    last_error = 'lease_lost'
                WHERE aggregate_id = ?
                  AND status IN ('pending', 'processing')
                """,
                (completed_at, version),
            )
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def fail_outbox_event(
        self,
        outbox_id: int,
        claim_token: str,
        claim_generation: int,
        *,
        failure: str,
        retry: bool,
    ) -> bool:
        outbox_id = _positive_integer(outbox_id, "outbox_id")
        token = _required_text(claim_token, "claim_token")
        generation = _positive_integer(claim_generation, "claim_generation")
        failure_value = _required_text(failure, "failure")
        if len(failure_value) > 128:
            raise ValueError("failure is too long")
        if not isinstance(retry, bool):
            raise ValueError("retry must be a boolean")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT aggregate_id, attempt_count,
                       element_request_id,
                       element_request_generation
                FROM publication_outbox
                WHERE id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (outbox_id, token, generation),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return False
            now = datetime.now(UTC)
            if retry:
                delays = (15, 30, 60)
                delay = delays[min(int(row["attempt_count"]), len(delays)) - 1]
                status = "pending"
                completed_at = None
                next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
            else:
                status = "publication_failed"
                completed_at = now.isoformat()
                next_attempt_at = now.isoformat()
                self.connection.execute(
                    """
                    UPDATE selector_versions
                    SET status = 'publication_failed'
                    WHERE id = ?
                    """,
                    (row["aggregate_id"],),
                )
                if row["element_request_id"]:
                    _fail_staged_element_request(
                        self.connection,
                        request_id=str(row["element_request_id"]),
                        request_generation=int(
                            row["element_request_generation"]
                        ),
                        version_id=str(row["aggregate_id"]),
                        error_code=failure_value,
                        completed_at=now.isoformat(),
                    )
            cursor = self.connection.execute(
                """
                UPDATE publication_outbox
                SET status = ?,
                    next_attempt_at = ?,
                    completed_at = ?,
                    lease_until = NULL,
                    last_error = ?
                WHERE id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (
                    status,
                    next_attempt_at,
                    completed_at,
                    failure_value,
                    outbox_id,
                    token,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return False
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def get_version(self, version_id: str) -> dict[str, object] | None:
        version = _required_text(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM selector_versions WHERE id = ?",
            (version,),
        ).fetchone()
        return _version_row(row) if row is not None else None

    def managed_element_version_history(
        self,
        element_id: str,
        *,
        site: str = "tiktok",
        environment: str = "production",
        limit: int = 100,
    ) -> list[dict[str, object]]:
        selected_id = _gate_text(element_id, "element_id")
        selected_site = _key_segment(site, "site")
        selected_environment = _key_segment(environment, "environment")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be between 1 and 100")
        rows = self.connection.execute(
            """
            SELECT id, status, base_version_id, bundle_hash, bundle_json,
                   created_at, validated_at, published_at
            FROM selector_versions
            WHERE site = ? AND environment = ?
            ORDER BY COALESCE(published_at, validated_at, created_at) DESC,
                     id DESC
            """,
            (selected_site, selected_environment),
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            bundle = _decode_json_object(
                row["bundle_json"],
                "selector version bundle",
            )
            elements = bundle.get("elements")
            if not isinstance(elements, Mapping) or selected_id not in elements:
                continue
            result.append(
                {
                    "version_id": str(row["id"]),
                    "status": str(row["status"]),
                    "base_version_id": str(row["base_version_id"]),
                    "bundle_hash": str(row["bundle_hash"]),
                    "created_at": str(row["created_at"]),
                    "validated_at": row["validated_at"],
                    "published_at": row["published_at"],
                }
            )
            if len(result) >= limit:
                break
        return result

    def last_published_version(
        self,
        *,
        site: str = "tiktok",
        environment: str = "production",
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM selector_versions
            WHERE site = ?
              AND environment = ?
              AND status = 'published'
            ORDER BY published_at DESC, created_at DESC
            LIMIT 1
            """,
            (
                _key_segment(site, "site"),
                _key_segment(environment, "environment"),
            ),
        ).fetchone()
        return _version_row(row) if row is not None else None

    def mark_version_publication_failed(self, version_id: str) -> bool:
        version = _required_text(version_id, "version_id")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE selector_versions
                SET status = 'publication_failed'
                WHERE id = ? AND status = 'published'
                """,
                (version,),
            )
        return cursor.rowcount == 1

    def upsert_managed_element_projection(
        self,
        *,
        element_id: str,
        display_name: str,
        management_source: str,
        published_status: str,
        draft_status: str | None,
        active_version_id: str,
        scope: str,
        primary_locator_type: str,
        last_validated_at: str | None,
        actor_user_id: int,
        actor_username: str,
    ) -> int:
        selected_id = _gate_text(element_id, "element_id")
        selected_name = _gate_text(display_name, "display_name")
        if management_source not in {
            "automatic",
            "legacy_manual",
            "disabled",
        }:
            raise ValueError("management_source is invalid")
        if published_status not in {
            "healthy",
            "using_lkg",
            "failed",
            "probe_unavailable",
            "disabled",
        }:
            raise ValueError("published_status is invalid")
        if draft_status not in {
            None,
            "draft",
            "queued",
            "probing",
            "validating",
        }:
            raise ValueError("draft_status is invalid")
        active_version = _required_text_or_empty(
            active_version_id,
            "active_version_id",
        )
        if len(active_version) > 128:
            raise ValueError("active_version_id is invalid")
        if scope not in ELEMENT_SCOPES:
            raise ValueError("scope is invalid")
        locator_type = _required_text_or_empty(
            primary_locator_type,
            "primary_locator_type",
        )
        if len(locator_type) > 64:
            raise ValueError("primary_locator_type is invalid")
        validated_at = (
            None
            if last_validated_at is None
            else _iso_timestamp(last_validated_at, "last_validated_at")
        )
        actor_id = _positive_integer(actor_user_id, "actor_user_id")
        actor_name = _gate_text(actor_username, "actor_username")
        now = _utc_now()

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT revision, created_at FROM managed_elements WHERE id = ?",
                (selected_id,),
            ).fetchone()
            if current is not None:
                _assert_no_element_request_in_progress(
                    self.connection,
                    selected_id,
                )
            revision = int(current["revision"]) + 1 if current is not None else 1
            created_at = str(current["created_at"]) if current is not None else now
            self.connection.execute(
                """
                INSERT INTO managed_elements (
                    id,
                    display_name,
                    management_source,
                    published_status,
                    draft_status,
                    active_version_id,
                    scope,
                    primary_locator_type,
                    last_validated_at,
                    revision,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    management_source = excluded.management_source,
                    published_status = excluded.published_status,
                    draft_status = excluded.draft_status,
                    active_version_id = excluded.active_version_id,
                    scope = excluded.scope,
                    primary_locator_type = excluded.primary_locator_type,
                    last_validated_at = excluded.last_validated_at,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    selected_id,
                    selected_name,
                    management_source,
                    published_status,
                    draft_status,
                    active_version,
                    scope,
                    locator_type,
                    validated_at,
                    revision,
                    created_at,
                    now,
                ),
            )
            self.connection.execute(
                """
                UPDATE element_catalog_state
                SET revision = revision + 1
                WHERE singleton = 1
                """
            )
            self.connection.execute(
                """
                INSERT INTO selector_management_audit_events (
                    actor_user_id,
                    actor_username,
                    event_type,
                    target_type,
                    target_id,
                    result,
                    details_json,
                    created_at
                ) VALUES (?, ?, 'managed_element_projected', 'element', ?,
                          'succeeded', ?, ?)
                """,
                (
                    actor_id,
                    actor_name,
                    selected_id,
                    _json(
                        {
                            "management_source": management_source,
                            "published_status": published_status,
                            "draft_status": draft_status,
                            "scope": scope,
                        }
                    ),
                    now,
                ),
            )
            self.connection.commit()
            return revision
        except BaseException:
            self.connection.rollback()
            raise

    def catalog_revision(self) -> int:
        row = self.connection.execute(
            """
            SELECT revision
            FROM element_catalog_state
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("element catalog state is missing")
        return int(row["revision"])

    def managed_element_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT id FROM managed_elements ORDER BY id"
        ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    def selector_probe_overview(
        self,
        *,
        site: str,
        environment: str,
    ) -> dict[str, object]:
        selected_site = _gate_text(site, "site")
        selected_environment = _gate_text(environment, "environment")
        self.connection.execute("BEGIN")
        try:
            revision = self.catalog_revision()
            health_row = self.connection.execute(
                """
                SELECT failure_started_at, retry_count, next_retry_at,
                       last_validated_at
                FROM probe_health_state
                WHERE site = ? AND environment = ?
                """,
                (selected_site, selected_environment),
            ).fetchone()
            version_row = self.connection.execute(
                """
                SELECT id, published_at
                FROM selector_versions
                WHERE site = ? AND environment = ? AND status = 'published'
                ORDER BY published_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (selected_site, selected_environment),
            ).fetchone()
            successful_run = self.connection.execute(
                """
                SELECT id, status, scheduled_for, started_at, finished_at,
                       published_version_after
                FROM probe_runs
                WHERE status IN ('completed', 'healthy', 'published')
                ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            count_rows = self.connection.execute(
                """
                SELECT COALESCE(draft_status, published_status) AS status,
                       COUNT(*) AS count
                FROM managed_elements
                GROUP BY COALESCE(draft_status, published_status)
                """
            ).fetchall()
            total_row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM managed_elements"
            ).fetchone()
            priority_rows = self.connection.execute(
                """
                SELECT m.id, m.display_name, m.management_source,
                       m.published_status, m.draft_status, m.scope,
                       m.primary_locator_type, m.last_validated_at,
                       m.revision,
                       (
                           SELECT COUNT(DISTINCT dependency.strategy_id)
                           FROM strategy_dependencies dependency
                           WHERE dependency.alias = m.id
                       ) AS dependency_count
                FROM managed_elements m
                ORDER BY
                    CASE
                        WHEN m.published_status = 'failed' THEN 1
                        WHEN m.published_status = 'using_lkg' THEN 2
                        WHEN m.draft_status IS NOT NULL THEN 3
                        WHEN m.published_status = 'probe_unavailable' THEN 4
                        ELSE 5
                    END,
                    CASE
                        WHEN m.last_validated_at IS NULL THEN 0
                        ELSE 1
                    END,
                    m.last_validated_at,
                    m.display_name COLLATE NOCASE,
                    m.id
                LIMIT 5
                """
            ).fetchall()
            gate_rows = self.connection.execute(
                """
                SELECT source, COUNT(DISTINCT strategy_id) AS count
                FROM strategy_gate_reasons
                WHERE cleared_at IS NULL
                  AND (
                      source = 'manual'
                      OR (
                          source = 'probe'
                          AND site = ?
                          AND environment = ?
                      )
                  )
                GROUP BY source
                """,
                (selected_site, selected_environment),
            ).fetchall()
            alert_count_rows = self.connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM probe_alerts
                WHERE site = ? AND environment = ?
                GROUP BY status
                """,
                (selected_site, selected_environment),
            ).fetchall()
            latest_alert = self.connection.execute(
                """
                SELECT id, status, failure_class, last_seen_at,
                       occurrence_count
                FROM probe_alerts
                WHERE site = ? AND environment = ?
                ORDER BY last_seen_at DESC, id DESC
                LIMIT 1
                """,
                (selected_site, selected_environment),
            ).fetchone()
            webhook_row = self.connection.execute(
                """
                SELECT webhook.status, webhook.event_type,
                       webhook.attempt_count, webhook.created_at,
                       webhook.completed_at
                FROM webhook_outbox webhook
                JOIN probe_alerts alert ON alert.id = webhook.alert_id
                WHERE alert.site = ? AND alert.environment = ?
                ORDER BY webhook.created_at DESC, webhook.id DESC
                LIMIT 1
                """,
                (selected_site, selected_environment),
            ).fetchone()
            audit_rows = self.connection.execute(
                """
                SELECT event_type, target_type, target_id, result, created_at
                FROM selector_management_audit_events
                ORDER BY created_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()
            run_rows = self.connection.execute(
                """
                SELECT id, status, COALESCE(finished_at, started_at) AS created_at
                FROM probe_runs
                ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
                LIMIT 5
                """
            ).fetchall()
            version_rows = self.connection.execute(
                """
                SELECT id, status, COALESCE(published_at, created_at) AS created_at
                FROM selector_versions
                WHERE site = ? AND environment = ?
                ORDER BY COALESCE(published_at, created_at) DESC, id DESC
                LIMIT 5
                """,
                (selected_site, selected_environment),
            ).fetchall()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

        health = {
            "status": "never_validated",
            "failure_started_at": "",
            "retry_count": 0,
            "next_retry_at": "",
            "last_validated_at": "",
        }
        if health_row is not None:
            health.update(
                {
                    "failure_started_at": str(
                        health_row["failure_started_at"]
                    ),
                    "retry_count": int(health_row["retry_count"]),
                    "next_retry_at": str(health_row["next_retry_at"]),
                    "last_validated_at": str(
                        health_row["last_validated_at"]
                    ),
                }
            )
            health["status"] = (
                "degraded"
                if health["failure_started_at"]
                or health["retry_count"] > 0
                else (
                    "healthy"
                    if health["last_validated_at"]
                    else "never_validated"
                )
            )
        counts = {
            "all": int(total_row["count"]),
            "healthy": 0,
            "using_lkg": 0,
            "draft": 0,
            "queued": 0,
            "probing": 0,
            "validating": 0,
            "failed": 0,
            "probe_unavailable": 0,
            "disabled": 0,
        }
        for row in count_rows:
            status = str(row["status"])
            if status in counts:
                counts[status] = int(row["count"])
        gate_counts = {"automatic": 0, "manual": 0}
        for row in gate_rows:
            gate_counts[
                "automatic" if row["source"] == "probe" else "manual"
            ] = int(row["count"])
        alert_counts = {"open": 0, "acknowledged": 0, "resolved": 0}
        for row in alert_count_rows:
            alert_counts[str(row["status"])] = int(row["count"])
        events = [
            {
                "event_type": str(row["event_type"]),
                "target_type": str(row["target_type"]),
                "target_id": str(row["target_id"]),
                "result": str(row["result"]),
                "created_at": str(row["created_at"]),
            }
            for row in audit_rows
        ]
        events.extend(
            {
                "event_type": "probe_run",
                "target_type": "probe_run",
                "target_id": str(row["id"]),
                "result": str(row["status"]),
                "created_at": str(row["created_at"]),
            }
            for row in run_rows
        )
        events.extend(
            {
                "event_type": "selector_version",
                "target_type": "selector_version",
                "target_id": str(row["id"]),
                "result": str(row["status"]),
                "created_at": str(row["created_at"]),
            }
            for row in version_rows
        )
        events.sort(
            key=lambda item: (item["created_at"], item["target_id"]),
            reverse=True,
        )
        return {
            "health": health,
            "current_version": (
                {
                    "id": str(version_row["id"]),
                    "published_at": version_row["published_at"],
                }
                if version_row is not None
                else None
            ),
            "last_successful": (
                dict(successful_run)
                if successful_run is not None
                else None
            ),
            "element_counts": counts,
            "priority_elements": [dict(row) for row in priority_rows],
            "gate_counts": gate_counts,
            "alert_summary": {
                **alert_counts,
                "active": alert_counts["open"]
                + alert_counts["acknowledged"],
                "latest": (
                    dict(latest_alert)
                    if latest_alert is not None
                    else None
                ),
            },
            "webhook_status": (
                dict(webhook_row) if webhook_row is not None else None
            ),
            "recent_events": events[:10],
            "revision": revision,
        }

    def list_managed_element_rows(
        self,
        *,
        page: int,
        page_size: int,
        search: str,
        status: str,
        source: str,
        scope: str,
        referenced: str,
    ) -> tuple[tuple[sqlite3.Row, ...], int, int]:
        clauses: list[str] = []
        arguments: list[object] = []
        if search:
            pattern = f"%{_escape_like(search)}%"
            clauses.append(
                """
                (
                    m.display_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                    OR m.id LIKE ? ESCAPE '\\' COLLATE NOCASE
                    OR EXISTS (
                        SELECT 1
                        FROM strategy_dependencies search_dependency
                        WHERE search_dependency.alias = m.id
                          AND (
                              search_dependency.strategy_id
                                  LIKE ? ESCAPE '\\' COLLATE NOCASE
                              OR search_dependency.strategy_name
                                  LIKE ? ESCAPE '\\' COLLATE NOCASE
                          )
                    )
                )
                """
            )
            arguments.extend((pattern, pattern, pattern, pattern))
        if status == "draft":
            clauses.append("m.draft_status IS NOT NULL")
        elif status != "all":
            clauses.append(
                "m.draft_status IS NULL AND m.published_status = ?"
            )
            arguments.append(status)
        if source != "all":
            clauses.append("m.management_source = ?")
            arguments.append(source)
        if scope != "all":
            clauses.append("m.scope = ?")
            arguments.append(scope)
        if referenced == "yes":
            clauses.append(
                """
                EXISTS (
                    SELECT 1 FROM strategy_dependencies referenced_dependency
                    WHERE referenced_dependency.alias = m.id
                )
                """
            )
        elif referenced == "no":
            clauses.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM strategy_dependencies referenced_dependency
                    WHERE referenced_dependency.alias = m.id
                )
                """
            )
        where_sql = " AND ".join(f"({clause})" for clause in clauses) or "1 = 1"

        self.connection.execute("BEGIN")
        try:
            revision = self.catalog_revision()
            total_row = self.connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM managed_elements m
                WHERE {where_sql}
                """,
                arguments,
            ).fetchone()
            rows = self.connection.execute(
                f"""
                SELECT m.*,
                       (
                           SELECT COUNT(DISTINCT count_dependency.strategy_id)
                           FROM strategy_dependencies count_dependency
                           WHERE count_dependency.alias = m.id
                       ) AS dependency_count
                FROM managed_elements m
                WHERE {where_sql}
                ORDER BY
                    CASE
                        WHEN m.published_status = 'failed' THEN 1
                        WHEN m.published_status = 'using_lkg' THEN 2
                        WHEN m.draft_status IS NOT NULL THEN 3
                        WHEN m.published_status = 'probe_unavailable' THEN 4
                        ELSE 5
                    END,
                    CASE
                        WHEN m.last_validated_at IS NULL THEN 0
                        ELSE 1
                    END,
                    m.last_validated_at,
                    m.display_name COLLATE BINARY,
                    m.id
                LIMIT ? OFFSET ?
                """,
                (
                    *arguments,
                    page_size,
                    (page - 1) * page_size,
                ),
            ).fetchall()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return tuple(rows), int(total_row["total"]), revision

    def get_managed_element_row(
        self,
        element_id: str,
    ) -> sqlite3.Row | None:
        selected_id = _gate_text(element_id, "element_id")
        return self.connection.execute(
            """
            SELECT m.*,
                   (
                       SELECT COUNT(DISTINCT dependency.strategy_id)
                       FROM strategy_dependencies dependency
                       WHERE dependency.alias = m.id
                   ) AS dependency_count
            FROM managed_elements m
            WHERE m.id = ?
            """,
            (selected_id,),
        ).fetchone()

    def managed_element_dependency_rows(
        self,
        element_id: str,
    ) -> tuple[sqlite3.Row, ...]:
        selected_id = _gate_text(element_id, "element_id")
        rows = self.connection.execute(
            """
            SELECT alias, strategy_id, action_id, action_type, strategy_name
            FROM strategy_dependencies
            WHERE alias = ?
            ORDER BY strategy_id, action_id
            """,
            (selected_id,),
        ).fetchall()
        return tuple(rows)

    def managed_element_draft_row(
        self,
        element_id: str,
    ) -> sqlite3.Row | None:
        selected_id = _gate_text(element_id, "element_id")
        return self.connection.execute(
            """
            SELECT *
            FROM element_drafts
            WHERE element_id = ?
            """,
            (selected_id,),
        ).fetchone()

    def create_managed_element_draft(
        self,
        *,
        element_id: str,
        display_name: str,
        contract: Mapping[str, object],
        scope: str,
        actor_user_id: int,
        actor_username: str,
    ) -> int:
        selected_id = _gate_text(element_id, "element_id")
        selected_name = _gate_text(display_name, "display_name")
        if not isinstance(contract, Mapping):
            raise ValueError("contract must be an object")
        contract_json = _json(dict(contract))
        if scope not in ELEMENT_SCOPES:
            raise ValueError("scope is invalid")
        actor_id, actor_name = _selector_actor(
            actor_user_id,
            actor_username,
        )
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.connection.execute(
                "SELECT 1 FROM managed_elements WHERE id = ?",
                (selected_id,),
            ).fetchone() is not None:
                raise ElementAlreadyExistsError(selected_id)
            self.connection.execute(
                """
                INSERT INTO managed_elements (
                    id,
                    display_name,
                    management_source,
                    published_status,
                    draft_status,
                    active_version_id,
                    scope,
                    primary_locator_type,
                    last_validated_at,
                    revision,
                    created_at,
                    updated_at
                ) VALUES (?, ?, 'automatic', 'probe_unavailable', 'draft',
                          '', ?, '', NULL, 1, ?, ?)
                """,
                (selected_id, selected_name, scope, now, now),
            )
            self.connection.execute(
                """
                INSERT INTO element_drafts (
                    element_id,
                    contract_json,
                    candidates_json,
                    validation_json,
                    base_version_id,
                    revision,
                    created_by,
                    created_by_username,
                    created_at,
                    updated_at
                ) VALUES (?, ?, '[]', '{}', '', 1, ?, ?, ?, ?)
                """,
                (
                    selected_id,
                    contract_json,
                    actor_id,
                    actor_name,
                    now,
                    now,
                ),
            )
            _bump_element_catalog_revision(self.connection)
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=actor_id,
                actor_username=actor_name,
                event_type="element_created",
                target_id=selected_id,
                details={"scope": scope},
                created_at=now,
            )
            self.connection.commit()
            return 1
        except BaseException:
            self.connection.rollback()
            raise

    def seed_legacy_elements(
        self,
        elements: Mapping[str, object],
        contracts: Mapping[str, object],
    ) -> int:
        normalized = normalize_element_definitions(dict(elements))
        if not isinstance(contracts, Mapping):
            raise ValueError("contracts must be an object")
        now = _utc_now()
        inserted = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for alias, definition in normalized.items():
                if self.connection.execute(
                    "SELECT 1 FROM managed_elements WHERE id = ?",
                    (alias,),
                ).fetchone() is not None:
                    continue
                contract_value = contracts.get(alias)
                public_contract = getattr(contract_value, "public_dict", None)
                if callable(public_contract):
                    contract = public_contract()
                elif isinstance(contract_value, Mapping):
                    contract = dict(contract_value)
                else:
                    scope = str(definition["scope"])
                    preferred = [
                        str(locator.get("name"))
                        for locator in definition["locators"]
                        if locator.get("type") == "attribute"
                        and isinstance(locator.get("name"), str)
                    ]
                    contract = {
                        "intent": f"locate {alias}",
                        "required_state": (
                            "comment_panel_open"
                            if scope == "visible_comment_panel"
                            else "feed_ready"
                        ),
                        "scope": scope,
                        "accepted_roles": ["button"],
                        "accepted_names": {
                            "mode": "exact",
                            "values": [alias],
                        },
                        "preferred_attributes": preferred
                        or ["data-e2e", "aria-label"],
                        "postcondition": "",
                        "probe_action": "inspect_only",
                    }
                scope = str(contract.get("scope") or definition["scope"])
                if scope not in ELEMENT_SCOPES:
                    scope = str(definition["scope"])
                locators = [dict(item) for item in definition["locators"]]
                primary_type = str(locators[0].get("type") or "")
                self.connection.execute(
                    """
                    INSERT INTO managed_elements (
                        id, display_name, management_source,
                        published_status, draft_status, active_version_id,
                        scope, primary_locator_type, last_validated_at,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, 'legacy_manual', 'using_lkg', 'draft',
                              '', ?, ?, NULL, 1, ?, ?)
                    """,
                    (
                        alias,
                        alias,
                        scope,
                        primary_type,
                        now,
                        now,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO element_drafts (
                        element_id, contract_json, candidates_json,
                        validation_json, base_version_id, revision,
                        created_by, created_by_username, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, '{}', '', 1, 1, 'system', ?, ?)
                    """,
                    (
                        alias,
                        _json(contract),
                        _json(locators),
                        now,
                        now,
                    ),
                )
                _insert_selector_management_audit(
                    self.connection,
                    actor_user_id=1,
                    actor_username="system",
                    event_type="legacy_element_seeded",
                    target_id=alias,
                    details={"scope": scope},
                    created_at=now,
                )
                inserted += 1
            if inserted:
                _bump_element_catalog_revision(self.connection)
            self.connection.commit()
            return inserted
        except BaseException:
            self.connection.rollback()
            raise

    def update_managed_element_draft(
        self,
        *,
        element_id: str,
        contract: Mapping[str, object],
        scope: str,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str,
    ) -> int:
        selected_id = _gate_text(element_id, "element_id")
        if not isinstance(contract, Mapping):
            raise ValueError("contract must be an object")
        contract_json = _json(dict(contract))
        if scope not in ELEMENT_SCOPES:
            raise ValueError("scope is invalid")
        expected = _positive_integer(expected_revision, "expected_revision")
        actor_id, actor_name = _selector_actor(
            actor_user_id,
            actor_username,
        )
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT revision FROM managed_elements WHERE id = ?",
                (selected_id,),
            ).fetchone()
            if row is None:
                raise ElementNotFoundError(selected_id)
            _assert_no_element_request_in_progress(
                self.connection,
                selected_id,
            )
            if int(row["revision"]) != expected:
                raise StaleElementRevisionError(selected_id)
            revision = expected + 1
            self.connection.execute(
                """
                UPDATE managed_elements
                SET draft_status = 'draft',
                    scope = ?,
                    revision = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (scope, revision, now, selected_id),
            )
            current_draft = self.connection.execute(
                """
                SELECT revision, created_at
                FROM element_drafts
                WHERE element_id = ?
                """,
                (selected_id,),
            ).fetchone()
            draft_revision = (
                int(current_draft["revision"]) + 1
                if current_draft is not None
                else 1
            )
            created_at = (
                str(current_draft["created_at"])
                if current_draft is not None
                else now
            )
            self.connection.execute(
                """
                INSERT INTO element_drafts (
                    element_id,
                    contract_json,
                    candidates_json,
                    validation_json,
                    base_version_id,
                    revision,
                    created_by,
                    created_by_username,
                    created_at,
                    updated_at
                ) VALUES (?, ?, '[]', '{}', '', ?, ?, ?, ?, ?)
                ON CONFLICT(element_id) DO UPDATE SET
                    contract_json = excluded.contract_json,
                    validation_json = '{}',
                    revision = excluded.revision,
                    created_by = excluded.created_by,
                    created_by_username = excluded.created_by_username,
                    updated_at = excluded.updated_at
                """,
                (
                    selected_id,
                    contract_json,
                    draft_revision,
                    actor_id,
                    actor_name,
                    created_at,
                    now,
                ),
            )
            _bump_element_catalog_revision(self.connection)
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=actor_id,
                actor_username=actor_name,
                event_type="element_draft_updated",
                target_id=selected_id,
                details={"revision": revision, "scope": scope},
                created_at=now,
            )
            self.connection.commit()
            return revision
        except BaseException:
            self.connection.rollback()
            raise

    def delete_managed_element(
        self,
        *,
        element_id: str,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str,
    ) -> None:
        selected_id = _gate_text(element_id, "element_id")
        expected = _positive_integer(expected_revision, "expected_revision")
        actor_id, actor_name = _selector_actor(
            actor_user_id,
            actor_username,
        )
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT revision FROM managed_elements WHERE id = ?",
                (selected_id,),
            ).fetchone()
            if row is None:
                raise ElementNotFoundError(selected_id)
            _assert_no_element_request_in_progress(
                self.connection,
                selected_id,
            )
            if int(row["revision"]) != expected:
                raise StaleElementRevisionError(selected_id)
            if self.connection.execute(
                """
                SELECT 1
                FROM strategy_dependencies
                WHERE alias = ?
                LIMIT 1
                """,
                (selected_id,),
            ).fetchone() is not None:
                raise ElementHasDependenciesError(selected_id)
            self.connection.execute(
                "DELETE FROM managed_elements WHERE id = ?",
                (selected_id,),
            )
            _bump_element_catalog_revision(self.connection)
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=actor_id,
                actor_username=actor_name,
                event_type="element_deleted",
                target_id=selected_id,
                details={"revision": expected},
                created_at=now,
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def migrate_legacy_element(
        self,
        *,
        element_id: str,
        display_name: str,
        definition: Mapping[str, object],
        expected_revision: int,
        actor_user_id: int,
        actor_username: str,
    ) -> int:
        selected_id = _gate_text(element_id, "element_id")
        selected_name = _gate_text(display_name, "display_name")
        if (
            not isinstance(definition, Mapping)
            or definition.get("scope") not in ELEMENT_SCOPES
            or not isinstance(definition.get("locators"), list)
            or not definition["locators"]
        ):
            raise ValueError("legacy definition is invalid")
        expected = _nonnegative_integer(
            expected_revision,
            "expected_revision",
        )
        candidates_json = _json(list(definition["locators"]))
        scope = str(definition["scope"])
        primary_locator_type = str(definition["locators"][0]["type"])
        actor_id, actor_name = _selector_actor(
            actor_user_id,
            actor_username,
        )
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT revision, management_source, published_status,
                       active_version_id, last_validated_at, created_at
                FROM managed_elements
                WHERE id = ?
                """,
                (selected_id,),
            ).fetchone()
            current_revision = int(row["revision"]) if row is not None else 0
            if row is not None:
                _assert_no_element_request_in_progress(
                    self.connection,
                    selected_id,
                )
            if current_revision != expected:
                raise StaleElementRevisionError(selected_id)
            if (
                row is not None
                and row["management_source"] not in {
                    "legacy_manual",
                    "disabled",
                }
            ):
                raise ElementMigrationConflictError(selected_id)
            if self.connection.execute(
                "SELECT 1 FROM element_drafts WHERE element_id = ?",
                (selected_id,),
            ).fetchone() is not None:
                raise ElementMigrationConflictError(selected_id)
            revision = current_revision + 1
            published_status = (
                str(row["published_status"]) if row is not None else "healthy"
            )
            active_version_id = (
                str(row["active_version_id"]) if row is not None else ""
            )
            last_validated_at = (
                row["last_validated_at"] if row is not None else None
            )
            created_at = (
                str(row["created_at"]) if row is not None else now
            )
            self.connection.execute(
                """
                INSERT INTO managed_elements (
                    id,
                    display_name,
                    management_source,
                    published_status,
                    draft_status,
                    active_version_id,
                    scope,
                    primary_locator_type,
                    last_validated_at,
                    revision,
                    created_at,
                    updated_at
                ) VALUES (?, ?, 'legacy_manual', ?, 'draft', ?, ?, ?, ?,
                          ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    management_source = 'legacy_manual',
                    draft_status = 'draft',
                    scope = excluded.scope,
                    primary_locator_type = excluded.primary_locator_type,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    selected_id,
                    selected_name,
                    published_status,
                    active_version_id,
                    scope,
                    primary_locator_type,
                    last_validated_at,
                    revision,
                    created_at,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO element_drafts (
                    element_id,
                    contract_json,
                    candidates_json,
                    validation_json,
                    base_version_id,
                    revision,
                    created_by,
                    created_by_username,
                    created_at,
                    updated_at
                ) VALUES (?, '{}', ?, '{"status":"observe_only"}', ?, 1,
                          ?, ?, ?, ?)
                """,
                (
                    selected_id,
                    candidates_json,
                    active_version_id,
                    actor_id,
                    actor_name,
                    now,
                    now,
                ),
            )
            _bump_element_catalog_revision(self.connection)
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=actor_id,
                actor_username=actor_name,
                event_type="element_legacy_migrated",
                target_id=selected_id,
                details={
                    "candidate_count": len(definition["locators"]),
                    "scope": scope,
                },
                created_at=now,
            )
            self.connection.commit()
            return revision
        except BaseException:
            self.connection.rollback()
            raise

    def reserve_element_request(
        self,
        *,
        element_id: str,
        request_type: str,
        request_id: str,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str,
    ) -> dict[str, object]:
        selected_id = _gate_text(element_id, "element_id")
        if request_type not in {"probe", "validate"}:
            raise ValueError("request_type is invalid")
        selected_request_id = _gate_text(request_id, "request_id")
        expected = _positive_integer(expected_revision, "expected_revision")
        actor_id, actor_name = _selector_actor(
            actor_user_id,
            actor_username,
        )
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT managed.revision, draft.contract_json
                FROM managed_elements managed
                LEFT JOIN element_drafts draft
                    ON draft.element_id = managed.id
                WHERE managed.id = ?
                """,
                (selected_id,),
            ).fetchone()
            if row is None:
                raise ElementNotFoundError(selected_id)
            _assert_no_element_request_in_progress(
                self.connection,
                selected_id,
            )
            if int(row["revision"]) != expected:
                raise StaleElementRevisionError(selected_id)
            if row["contract_json"] is None:
                raise ValueError("element contract is required")
            contract = _decode_json_object(
                row["contract_json"],
                "element request contract",
            )
            if not contract:
                raise ValueError("element contract is required")
            if self.connection.execute(
                """
                SELECT 1
                FROM element_request_outbox
                WHERE request_id = ?
                """,
                (selected_request_id,),
            ).fetchone() is not None:
                raise ElementRequestConflictError(selected_request_id)
            contract_json = _json(contract)
            request_revision = expected + 1
            self.connection.execute(
                """
                UPDATE managed_elements
                SET draft_status = 'queued',
                    revision = ?,
                    updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (
                    request_revision,
                    now,
                    selected_id,
                    expected,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO element_request_outbox (
                    request_id,
                    request_type,
                    element_id,
                    expected_revision,
                    contract_json,
                    actor_user_id,
                    actor_username,
                    status,
                    next_attempt_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    selected_request_id,
                    request_type,
                    selected_id,
                    request_revision,
                    contract_json,
                    actor_id,
                    actor_name,
                    now,
                    now,
                    now,
                ),
            )
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=actor_id,
                actor_username=actor_name,
                event_type=(
                    "element_probe_requested"
                    if request_type == "probe"
                    else "element_validation_requested"
                ),
                target_id=selected_id,
                details={
                    "request_id": selected_request_id,
                    "revision": request_revision,
                },
                created_at=now,
            )
            _bump_element_catalog_revision(self.connection)
            self.connection.commit()
            return {
                "request_id": selected_request_id,
                "request_type": request_type,
                "element_id": selected_id,
                "expected_revision": request_revision,
                "contract": contract,
                "actor_user_id": actor_id,
                "actor_username": actor_name,
                "status": "pending",
                "attempt_count": 0,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def get_element_request(
        self,
        request_id: str,
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM element_request_outbox
            WHERE request_id = ?
            """,
            (_gate_text(request_id, "request_id"),),
        ).fetchone()
        return _element_request_row(row) if row is not None else None

    def claim_element_request(
        self,
        *,
        claim_token: str | None = None,
        now: datetime | None = None,
        lease_seconds: int = 120,
    ) -> dict[str, object] | None:
        token = (
            _gate_text(claim_token, "claim_token")
            if claim_token is not None
            else uuid.uuid4().hex
        )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
            or lease_seconds > 3600
        ):
            raise ValueError("lease_seconds is invalid")
        selected_now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = selected_now.isoformat()
        lease_until = (
            selected_now + timedelta(seconds=lease_seconds)
        ).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT request_id, request_type, element_id
                FROM element_request_outbox
                WHERE (
                    status = 'pending'
                    AND next_attempt_at <= ?
                ) OR (
                    status = 'processing'
                    AND lease_until IS NOT NULL
                    AND lease_until <= ?
                )
                ORDER BY created_at, request_id
                LIMIT 1
                """,
                (now_text, now_text),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                """
                UPDATE element_request_outbox
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    claim_token = ?,
                    claim_generation = claim_generation + 1,
                    lease_until = ?,
                    started_at = COALESCE(started_at, ?),
                    error_code = '',
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    token,
                    lease_until,
                    now_text,
                    now_text,
                    row["request_id"],
                ),
            )
            self.connection.execute(
                """
                UPDATE managed_elements
                SET draft_status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    (
                        "probing"
                        if row["request_type"] == "probe"
                        else "validating"
                    ),
                    now_text,
                    row["element_id"],
                ),
            )
            _bump_element_catalog_revision(self.connection)
            claimed = self.connection.execute(
                """
                SELECT *
                FROM element_request_outbox
                WHERE request_id = ?
                """,
                (row["request_id"],),
            ).fetchone()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return _element_request_row(claimed)

    def renew_element_request_claim(
        self,
        request_id: str,
        claim_token: str,
        claim_generation: int,
        *,
        now: datetime | None = None,
        lease_seconds: int = 120,
    ) -> bool:
        return self.guard_element_request_claim(
            request_id,
            claim_token,
            claim_generation,
            now=now,
            renew=True,
            lease_seconds=lease_seconds,
        )

    def guard_element_request_claim(
        self,
        request_id: str,
        claim_token: str,
        claim_generation: int,
        *,
        now: datetime | None = None,
        renew: bool = False,
        lease_seconds: int = 120,
    ) -> bool:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
            or lease_seconds > 3600
        ):
            raise ValueError("lease_seconds is invalid")
        if not isinstance(renew, bool):
            raise ValueError("renew must be a boolean")
        selected_now = (now or datetime.now(UTC)).astimezone(UTC)
        lease_until = (
            selected_now + timedelta(seconds=lease_seconds)
        ).isoformat()
        now_text = selected_now.isoformat()
        query = """
            SELECT request.expected_revision,
                   request.contract_json,
                   request.lease_until,
                   request.status,
                   managed.revision AS managed_revision,
                   draft.contract_json AS current_contract_json
            FROM element_request_outbox request
            LEFT JOIN managed_elements managed
                ON managed.id = request.element_id
            LEFT JOIN element_drafts draft
                ON draft.element_id = request.element_id
            WHERE request.request_id = ?
              AND request.status IN ('processing', 'publishing')
              AND request.claim_token = ?
              AND request.claim_generation = ?
        """
        arguments = (
            _gate_text(request_id, "request_id"),
            _gate_text(claim_token, "claim_token"),
            _positive_integer(claim_generation, "claim_generation"),
        )

        def is_owned(row: sqlite3.Row | None) -> bool:
            return bool(
                row is not None
                and (
                    row["status"] == "publishing"
                    or (
                        row["lease_until"] is not None
                        and str(row["lease_until"]) > now_text
                    )
                )
                and row["managed_revision"] is not None
                and int(row["managed_revision"])
                == int(row["expected_revision"])
                and row["current_contract_json"] == row["contract_json"]
            )

        if not renew:
            return is_owned(
                self.connection.execute(query, arguments).fetchone()
            )

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(query, arguments).fetchone()
            owned = is_owned(row)
            if not owned:
                self.connection.commit()
                return False
            if row["status"] == "publishing":
                self.connection.commit()
                return True
            if renew:
                cursor = self.connection.execute(
                    """
                    UPDATE element_request_outbox
                    SET lease_until = ?,
                        updated_at = ?
                    WHERE request_id = ?
                      AND status = 'processing'
                      AND claim_token = ?
                      AND claim_generation = ?
                      AND lease_until > ?
                    """,
                    (
                        lease_until,
                        now_text,
                        request_id,
                        claim_token,
                        claim_generation,
                        now_text,
                    ),
                )
                if cursor.rowcount != 1:
                    self.connection.rollback()
                    return False
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def element_request_claim_is_current(
        self,
        request_id: str,
        claim_token: str,
        claim_generation: int,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT request.expected_revision,
                   request.contract_json,
                   managed.revision AS managed_revision,
                   draft.contract_json AS current_contract_json
            FROM element_request_outbox request
            LEFT JOIN managed_elements managed
                ON managed.id = request.element_id
            LEFT JOIN element_drafts draft
                ON draft.element_id = request.element_id
            WHERE request.request_id = ?
              AND request.status = 'processing'
              AND request.claim_token = ?
              AND request.claim_generation = ?
            """,
            (
                _gate_text(request_id, "request_id"),
                _gate_text(claim_token, "claim_token"),
                _positive_integer(claim_generation, "claim_generation"),
            ),
        ).fetchone()
        return bool(
            row is not None
            and row["managed_revision"] is not None
            and int(row["managed_revision"]) == int(row["expected_revision"])
            and row["current_contract_json"] == row["contract_json"]
        )

    def element_request_publication_is_complete(
        self,
        request_id: str,
        claim_generation: int,
        version_id: str,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM element_request_outbox request
            JOIN selector_versions version
                ON version.id = request.staged_version_id
            WHERE request.request_id = ?
              AND request.request_type = 'validate'
              AND request.status = 'completed'
              AND request.claim_generation = ?
              AND request.staged_version_id = ?
              AND version.status = 'published'
              AND version.element_request_id = request.request_id
              AND version.element_request_generation =
                    request.claim_generation
            """,
            (
                _gate_text(request_id, "request_id"),
                _positive_integer(claim_generation, "claim_generation"),
                _required_text(version_id, "version_id"),
            ),
        ).fetchone()
        return row is not None

    def abort_disabled_element_publications(
        self,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        code = _gate_text(error_code, "error_code")
        if code not in {"rollout_disabled", "probe_disabled"}:
            raise ValueError("unsupported rollout abort code")
        selected_now = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        aborted = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                """
                SELECT publication.id,
                       publication.aggregate_id,
                       publication.status,
                       publication.claim_generation,
                       publication.lease_until,
                       publication.element_request_id,
                       publication.element_request_generation
                FROM publication_outbox publication
                JOIN element_request_outbox request
                    ON request.request_id =
                        publication.element_request_id
                WHERE request.status = 'publishing'
                  AND request.staged_version_id =
                        publication.aggregate_id
                  AND request.claim_generation =
                        publication.element_request_generation
                  AND publication.status IN ('pending', 'processing')
                ORDER BY publication.id
                """
            ).fetchall()
            for row in rows:
                cancellable = row["status"] == "pending"
                if not cancellable:
                    continue
                cursor = self.connection.execute(
                    """
                    UPDATE publication_outbox
                    SET status = 'cancelled',
                        completed_at = ?,
                        lease_until = NULL,
                        last_error = ?
                    WHERE id = ?
                      AND status = ?
                      AND claim_generation = ?
                      AND status = 'pending'
                    """,
                    (
                        selected_now,
                        code,
                        row["id"],
                        row["status"],
                        row["claim_generation"],
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                self.connection.execute(
                    """
                    UPDATE selector_versions
                    SET status = 'cancelled'
                    WHERE id = ?
                      AND status = 'validated'
                      AND element_request_id = ?
                      AND element_request_generation = ?
                    """,
                    (
                        row["aggregate_id"],
                        row["element_request_id"],
                        row["element_request_generation"],
                    ),
                )
                _fail_staged_element_request(
                    self.connection,
                    request_id=str(row["element_request_id"]),
                    request_generation=int(
                        row["element_request_generation"]
                    ),
                    version_id=str(row["aggregate_id"]),
                    error_code=code,
                    completed_at=selected_now,
                )
                aborted += 1
            inflight = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM element_request_outbox request
                    JOIN publication_outbox publication
                        ON publication.element_request_id =
                            request.request_id
                    WHERE request.status = 'publishing'
                      AND publication.status = 'processing'
                      AND publication.lease_until > ?
                      AND request.staged_version_id =
                            publication.aggregate_id
                      AND request.claim_generation =
                            publication.element_request_generation
                    """,
                    (selected_now,),
                ).fetchone()[0]
            )
            indeterminate = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM element_request_outbox request
                    JOIN publication_outbox publication
                        ON publication.element_request_id =
                            request.request_id
                    WHERE request.status = 'publishing'
                      AND publication.status = 'processing'
                      AND publication.lease_until IS NOT NULL
                      AND publication.lease_until <= ?
                      AND request.staged_version_id =
                            publication.aggregate_id
                      AND request.claim_generation =
                            publication.element_request_generation
                    """,
                    (selected_now,),
                ).fetchone()[0]
            )
            self.connection.commit()
            return {
                "aborted": aborted,
                "inflight": inflight,
                "indeterminate": indeterminate,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def resolve_indeterminate_element_publications(
        self,
        *,
        active_version: str,
        active_bundle_hash: str,
        error_code: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        active_id = _required_text_or_empty(
            active_version,
            "active_version",
        )
        active_hash = _required_text_or_empty(
            active_bundle_hash,
            "active_bundle_hash",
        )
        code = _gate_text(error_code, "error_code")
        if code not in {"rollout_disabled", "probe_disabled"}:
            raise ValueError("unsupported rollout resolution code")
        selected_now = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        completed = 0
        cancelled = 0
        unresolved = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                """
                SELECT publication.id,
                       publication.aggregate_id,
                       publication.claim_generation,
                       publication.element_request_id,
                       publication.element_request_generation,
                       version.site,
                       version.environment,
                       version.bundle_hash,
                       request.request_type AS request_type,
                       request.element_id AS request_element_id,
                       request.actor_user_id AS request_actor_user_id,
                       request.actor_username AS request_actor_username,
                       request.attempt_count AS request_attempt_count,
                       request.staged_result_json
                            AS request_staged_result_json
                FROM publication_outbox publication
                JOIN element_request_outbox request
                    ON request.request_id =
                        publication.element_request_id
                JOIN selector_versions version
                    ON version.id = publication.aggregate_id
                WHERE request.status = 'publishing'
                  AND publication.status = 'processing'
                  AND publication.lease_until IS NOT NULL
                  AND publication.lease_until <= ?
                  AND request.staged_version_id =
                        publication.aggregate_id
                  AND request.claim_generation =
                        publication.element_request_generation
                  AND version.status = 'validated'
                ORDER BY publication.id
                """,
                (selected_now,),
            ).fetchall()
            for row in rows:
                version_id = str(row["aggregate_id"])
                if active_id == version_id:
                    if active_hash != row["bundle_hash"]:
                        unresolved += 1
                        continue
                    self.connection.execute(
                        """
                        UPDATE selector_versions
                        SET status = 'superseded'
                        WHERE site = ?
                          AND environment = ?
                          AND status = 'published'
                          AND id <> ?
                        """,
                        (row["site"], row["environment"], version_id),
                    )
                    self.connection.execute(
                        """
                        UPDATE selector_versions
                        SET status = 'published',
                            published_at = ?
                        WHERE id = ? AND status = 'validated'
                        """,
                        (selected_now, version_id),
                    )
                    cursor = self.connection.execute(
                        """
                        UPDATE publication_outbox
                        SET status = 'completed',
                            completed_at = ?,
                            lease_until = NULL
                        WHERE id = ?
                          AND status = 'processing'
                          AND claim_generation = ?
                          AND lease_until <= ?
                        """,
                        (
                            selected_now,
                            row["id"],
                            row["claim_generation"],
                            selected_now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        unresolved += 1
                        continue
                    _complete_staged_element_request(
                        self.connection,
                        row=row,
                        request_id=str(row["element_request_id"]),
                        version_id=version_id,
                        completed_at=selected_now,
                    )
                    completed += 1
                    continue
                cursor = self.connection.execute(
                    """
                    UPDATE publication_outbox
                    SET status = 'cancelled',
                        completed_at = ?,
                        lease_until = NULL,
                        last_error = ?
                    WHERE id = ?
                      AND status = 'processing'
                      AND claim_generation = ?
                      AND lease_until <= ?
                    """,
                    (
                        selected_now,
                        code,
                        row["id"],
                        row["claim_generation"],
                        selected_now,
                    ),
                )
                if cursor.rowcount != 1:
                    unresolved += 1
                    continue
                self.connection.execute(
                    """
                    UPDATE selector_versions
                    SET status = 'cancelled'
                    WHERE id = ? AND status = 'validated'
                    """,
                    (version_id,),
                )
                _fail_staged_element_request(
                    self.connection,
                    request_id=str(row["element_request_id"]),
                    request_generation=int(
                        row["element_request_generation"]
                    ),
                    version_id=version_id,
                    error_code=code,
                    completed_at=selected_now,
                )
                cancelled += 1
            self.connection.commit()
            return {
                "completed": completed,
                "cancelled": cancelled,
                "unresolved": unresolved,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def complete_element_request(
        self,
        request_id: str,
        claim_token: str,
        claim_generation: int,
        *,
        result: Mapping[str, object],
        now: datetime | None = None,
    ) -> bool:
        if not isinstance(result, Mapping):
            raise ValueError("element request result must be an object")
        request_key = _gate_text(request_id, "request_id")
        _json(dict(result))
        selected_now = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        token = _gate_text(claim_token, "claim_token")
        generation = _positive_integer(
            claim_generation,
            "claim_generation",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT request.*,
                       managed.revision AS managed_revision,
                       draft.contract_json AS current_contract_json
                FROM element_request_outbox request
                LEFT JOIN managed_elements managed
                    ON managed.id = request.element_id
                LEFT JOIN element_drafts draft
                    ON draft.element_id = request.element_id
                WHERE request.request_id = ?
                  AND request.status = 'processing'
                  AND request.claim_token = ?
                  AND request.claim_generation = ?
                """,
                (request_key, token, generation),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return False
            claim_is_current = (
                row["managed_revision"] is not None
                and int(row["managed_revision"])
                == int(row["expected_revision"])
                and row["current_contract_json"] == row["contract_json"]
            )
            if not claim_is_current:
                self.connection.execute(
                    """
                    UPDATE element_request_outbox
                    SET status = 'failed',
                        claim_token = '',
                        lease_until = NULL,
                        next_attempt_at = ?,
                        completed_at = ?,
                        error_code = 'stale_revision',
                        updated_at = ?
                    WHERE request_id = ?
                      AND status = 'processing'
                      AND claim_token = ?
                      AND claim_generation = ?
                    """,
                    (
                        selected_now,
                        selected_now,
                        selected_now,
                        request_key,
                        token,
                        generation,
                    ),
                )
                _insert_selector_management_audit(
                    self.connection,
                    actor_user_id=int(row["actor_user_id"]),
                    actor_username=str(row["actor_username"]),
                    event_type=f"element_{row['request_type']}_failed",
                    target_id=str(row["element_id"]),
                    details={
                        "request_id": request_key,
                        "attempt_count": int(row["attempt_count"]),
                        "error_code": "stale_revision",
                    },
                    created_at=selected_now,
                )
                self.connection.commit()
                return False
            safe_result = _safe_element_request_result(
                result,
                str(row["element_id"]),
            )
            request_type = str(row["request_type"])
            if request_type == "probe":
                publishable = safe_result.get("status") == "probe_completed"
            else:
                version = safe_result.get("new_version")
                publishable = (
                    safe_result.get("status") == "published"
                    and safe_result.get("published") is True
                    and safe_result.get("reconciled") is True
                    and isinstance(version, str)
                    and bool(version)
                )
            if not publishable:
                raise ValueError(
                    "element request result does not satisfy completion contract"
                )
            result_json = _json(safe_result)
            candidates = _element_result_candidates(
                safe_result,
                str(row["element_id"]),
            )
            validation = {
                "status": safe_result.get("status", "completed"),
                "last_validated_at": selected_now,
                "rounds": safe_result.get("rounds", []),
                "repairs": safe_result.get("repairs", []),
            }
            self.connection.execute(
                """
                UPDATE element_request_outbox
                SET status = 'completed',
                    claim_token = '',
                    lease_until = NULL,
                    completed_at = ?,
                    error_code = '',
                    result_json = ?,
                    updated_at = ?
                WHERE request_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (
                    selected_now,
                    result_json,
                    selected_now,
                    request_key,
                    token,
                    generation,
                ),
            )
            if candidates:
                self.connection.execute(
                    """
                    UPDATE element_drafts
                    SET candidates_json = ?,
                        validation_json = ?,
                        updated_at = ?
                    WHERE element_id = ?
                    """,
                    (
                        _json(candidates),
                        _json(validation),
                        selected_now,
                        row["element_id"],
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE element_drafts
                    SET validation_json = ?,
                        updated_at = ?
                    WHERE element_id = ?
                    """,
                    (
                        _json(validation),
                        selected_now,
                        row["element_id"],
                    ),
                )
            is_validation = request_type == "validate"
            version = str(
                safe_result.get("new_version")
                or safe_result.get("version")
                or ""
            )
            primary_locator_type = (
                str(candidates[0].get("type") or "")
                if candidates
                else ""
            )
            self.connection.execute(
                """
                UPDATE managed_elements
                SET management_source = CASE
                        WHEN ? THEN 'automatic'
                        ELSE management_source
                    END,
                    published_status = CASE
                        WHEN ? THEN 'healthy'
                        ELSE published_status
                    END,
                    draft_status = CASE
                        WHEN ? THEN NULL
                        ELSE 'draft'
                    END,
                    active_version_id = CASE
                        WHEN ? != '' THEN ?
                        ELSE active_version_id
                    END,
                    primary_locator_type = CASE
                        WHEN ? != '' THEN ?
                        ELSE primary_locator_type
                    END,
                    last_validated_at = CASE
                        WHEN ? THEN ?
                        ELSE last_validated_at
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    is_validation,
                    is_validation,
                    is_validation,
                    version,
                    version,
                    primary_locator_type,
                    primary_locator_type,
                    is_validation,
                    selected_now,
                    selected_now,
                    row["element_id"],
                ),
            )
            _bump_element_catalog_revision(self.connection)
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=int(row["actor_user_id"]),
                actor_username=str(row["actor_username"]),
                event_type=f"element_{row['request_type']}_completed",
                target_id=str(row["element_id"]),
                details={
                    "request_id": request_key,
                    "attempt_count": int(row["attempt_count"]),
                },
                created_at=selected_now,
            )
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def fail_element_request(
        self,
        request_id: str,
        claim_token: str,
        claim_generation: int,
        *,
        error_code: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> dict[str, object] | None:
        request_key = _gate_text(request_id, "request_id")
        token = _gate_text(claim_token, "claim_token")
        generation = _positive_integer(
            claim_generation,
            "claim_generation",
        )
        code = _gate_text(error_code, "error_code")
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a boolean")
        selected_now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = selected_now.isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT *
                FROM element_request_outbox
                WHERE request_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (request_key, token, generation),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            attempt_count = int(row["attempt_count"])
            terminal = (
                not retryable
                or attempt_count >= len(ELEMENT_REQUEST_RETRY_SECONDS)
            )
            if terminal:
                status = "failed"
                next_attempt_at = now_text
                completed_at = now_text
            else:
                status = "pending"
                next_attempt_at = (
                    selected_now
                    + timedelta(
                        seconds=ELEMENT_REQUEST_RETRY_SECONDS[
                            attempt_count - 1
                        ]
                    )
                ).isoformat()
                completed_at = None
            self.connection.execute(
                """
                UPDATE element_request_outbox
                SET status = ?,
                    claim_token = '',
                    lease_until = NULL,
                    next_attempt_at = ?,
                    completed_at = ?,
                    error_code = ?,
                    updated_at = ?
                WHERE request_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND claim_generation = ?
                """,
                (
                    status,
                    next_attempt_at,
                    completed_at,
                    code,
                    now_text,
                    request_key,
                    token,
                    generation,
                ),
            )
            validation = {
                "status": "failed" if terminal else "retrying",
                "failure_code": code,
                "request_id": request_key,
                "rounds": [],
                "repairs": [],
            }
            self.connection.execute(
                """
                UPDATE element_drafts
                SET validation_json = ?,
                    updated_at = ?
                WHERE element_id = ?
                """,
                (
                    _json(validation),
                    now_text,
                    row["element_id"],
                ),
            )
            self.connection.execute(
                """
                UPDATE managed_elements
                SET draft_status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    "draft" if terminal else "queued",
                    now_text,
                    row["element_id"],
                ),
            )
            _bump_element_catalog_revision(self.connection)
            if terminal:
                _insert_selector_management_audit(
                    self.connection,
                    actor_user_id=int(row["actor_user_id"]),
                    actor_username=str(row["actor_username"]),
                    event_type=f"element_{row['request_type']}_failed",
                    target_id=str(row["element_id"]),
                    details={
                        "request_id": request_key,
                        "attempt_count": attempt_count,
                        "error_code": code,
                    },
                    created_at=now_text,
                )
            self.connection.commit()
            return {
                "status": status,
                "terminal": terminal,
                "next_attempt_at": next_attempt_at,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def current_revision(self, resource: str) -> int:
        selected = _gate_text(resource, "resource")
        row = self.connection.execute(
            """
            SELECT revision
            FROM management_resource_revisions
            WHERE resource = ?
            """,
            (selected,),
        ).fetchone()
        if selected == "settings":
            return int(row["revision"]) if row is not None else 0
        managed_revision = int(row["revision"]) if row is not None else 0
        if selected == "runs":
            query = "SELECT COALESCE(MAX(id), 0) FROM probe_runs"
        elif selected == "versions":
            query = "SELECT COUNT(*) FROM selector_versions"
        elif selected == "gates":
            query = (
                "SELECT COALESCE(MAX(revision), 0) "
                "FROM strategy_gate_revisions"
            )
        elif selected == "alerts":
            query = "SELECT COALESCE(MAX(revision), 0) FROM probe_alerts"
        elif selected == "audit":
            query = (
                "SELECT COALESCE(MAX(id), 0) "
                "FROM selector_management_audit_events"
            )
        else:
            raise ValueError("unsupported resource")
        return managed_revision + int(
            self.connection.execute(query).fetchone()[0]
        )

    def bump_resource_revision(self, resource: str) -> int:
        selected = _gate_text(resource, "resource")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO management_resource_revisions(resource, revision)
                VALUES (?, 1)
                ON CONFLICT(resource) DO UPDATE
                SET revision = revision + 1
                """,
                (selected,),
            )
        return self.current_revision(selected)

    def stage_settings_publication(
        self,
        *,
        actor_user_id: int,
        actor_username: str,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
        expected_revision: int,
        candidate: Mapping[str, object],
        candidate_fingerprint: str,
        private_reference: str,
        reason: str,
        dangerous_changes: Sequence[str],
        now: datetime | None = None,
    ) -> dict[str, object]:
        actor_id, actor_name = _selector_actor(
            actor_user_id, actor_username
        )
        selected_operation = _gate_text(operation, "operation")
        key = _gate_text(idempotency_key, "idempotency_key")
        digest = _required_text(payload_hash, "payload_hash")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be non-negative")
        public_candidate = _private_safe_settings_candidate(candidate)
        fingerprint = _required_text(
            candidate_fingerprint, "candidate_fingerprint"
        )
        private_ref = _required_text(
            private_reference, "private_reference"
        )
        selected_reason = str(reason)[:500]
        changes = [
            _gate_text(item, "dangerous_change")
            for item in dangerous_changes
        ]
        selected_now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = selected_now.isoformat()
        intent_id = uuid.uuid4().hex
        staged_revision = expected_revision + 1
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            revision_row = self.connection.execute(
                """
                SELECT revision
                FROM management_resource_revisions
                WHERE resource = 'settings'
                """
            ).fetchone()
            current_revision = (
                int(revision_row["revision"])
                if revision_row is not None
                else 0
            )
            if current_revision != expected_revision:
                raise StaleManagementRevisionError(
                    "settings revision changed"
                )
            idempotency = self.connection.execute(
                """
                SELECT payload_hash, state
                FROM management_idempotency_cache
                WHERE actor_user_id = ? AND operation = ?
                  AND idempotency_key = ?
                """,
                (actor_id, selected_operation, key),
            ).fetchone()
            if (
                idempotency is None
                or str(idempotency["payload_hash"]) != digest
                or str(idempotency["state"]) != "pending"
            ):
                raise ManagementIdempotencyConflictError(
                    "settings publication reservation is not pending"
                )
            self.connection.execute(
                """
                INSERT INTO management_resource_revisions(resource, revision)
                VALUES ('settings', ?)
                ON CONFLICT(resource) DO UPDATE
                SET revision = excluded.revision
                """,
                (staged_revision,),
            )
            self.connection.execute(
                """
                INSERT INTO management_settings_publications (
                    id, actor_user_id, actor_username, operation,
                    idempotency_key, payload_hash, expected_revision,
                    staged_revision, candidate_json,
                    candidate_fingerprint, private_reference, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    intent_id,
                    actor_id,
                    actor_name,
                    selected_operation,
                    key,
                    digest,
                    expected_revision,
                    staged_revision,
                    _json(public_candidate),
                    fingerprint,
                    private_ref,
                    now_text,
                    now_text,
                ),
            )
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=actor_id,
                actor_username=actor_name,
                event_type="settings_update_staged",
                target_type="settings",
                target_id="selector_probe",
                details={
                    "intent_id": intent_id,
                    "reason": selected_reason,
                    "dangerous_changes": changes,
                    "expected_revision": expected_revision,
                    "staged_revision": staged_revision,
                    "candidate_fingerprint": fingerprint,
                },
                created_at=now_text,
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return {
            "id": intent_id,
            "expected_revision": expected_revision,
            "staged_revision": staged_revision,
            "candidate": public_candidate,
            "candidate_fingerprint": fingerprint,
            "status": "pending",
        }

    def pending_settings_publications(
        self,
    ) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM management_settings_publications
            WHERE status = 'pending'
            ORDER BY staged_revision, created_at, id
            """
        ).fetchall()
        return [
            {
                **dict(row),
                "candidate": _decode_json_object(
                    row["candidate_json"],
                    "settings publication candidate",
                ),
                "response": _decode_json_object(
                    row["response_json"],
                    "settings publication response",
                ),
            }
            for row in rows
        ]

    def complete_settings_publication(
        self,
        intent_id: str,
        *,
        private_reference: str,
        response: Mapping[str, object],
        status_code: int = 200,
        reconciled: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        selected_id = _gate_text(intent_id, "intent_id")
        private_ref = _required_text(
            private_reference, "private_reference"
        )
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 200 <= status_code <= 299
        ):
            raise ValueError("status_code is invalid")
        public_response = _private_safe_settings_candidate(response)
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT *
                FROM management_settings_publications
                WHERE id = ?
                """,
                (selected_id,),
            ).fetchone()
            if row is None:
                raise KeyError(selected_id)
            if str(row["private_reference"]) != private_ref:
                raise ValueError("settings publication does not match disk")
            if row["status"] == "failed":
                raise ManagementIdempotencyConflictError(
                    "settings publication already failed"
                )
            if row["status"] == "completed":
                self.connection.commit()
                return _decode_json_object(
                    row["response_json"],
                    "settings publication response",
                )
            encoded = _json(public_response)
            self.connection.execute(
                """
                UPDATE management_settings_publications
                SET status = 'completed', response_json = ?,
                    error_code = '', updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (encoded, now_text, now_text, selected_id),
            )
            cursor = self.connection.execute(
                """
                UPDATE management_idempotency_cache
                SET response_json = ?, status_code = ?,
                    state = 'completed'
                WHERE actor_user_id = ? AND operation = ?
                  AND idempotency_key = ? AND payload_hash = ?
                  AND state = 'pending'
                """,
                (
                    encoded,
                    status_code,
                    int(row["actor_user_id"]),
                    str(row["operation"]),
                    str(row["idempotency_key"]),
                    str(row["payload_hash"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ManagementIdempotencyConflictError(
                    "settings idempotency acknowledgement conflict"
                )
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=int(row["actor_user_id"]),
                actor_username=str(row["actor_username"]),
                event_type=(
                    "settings_update_reconciled"
                    if reconciled
                    else "settings_updated"
                ),
                target_type="settings",
                target_id="selector_probe",
                details={
                    "intent_id": selected_id,
                    "staged_revision": int(row["staged_revision"]),
                },
                created_at=now_text,
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return public_response

    def fail_settings_publication(
        self,
        intent_id: str,
        *,
        error_code: str,
        response: Mapping[str, object],
        status_code: int,
        now: datetime | None = None,
    ) -> None:
        selected_id = _gate_text(intent_id, "intent_id")
        code = _gate_text(error_code, "error_code")
        public_response = _private_safe_settings_candidate(response)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 400 <= status_code <= 599
        ):
            raise ValueError("status_code is invalid")
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT *
                FROM management_settings_publications
                WHERE id = ?
                """,
                (selected_id,),
            ).fetchone()
            if row is None:
                raise KeyError(selected_id)
            if row["status"] != "pending":
                self.connection.commit()
                return
            encoded = _json(public_response)
            self.connection.execute(
                """
                UPDATE management_settings_publications
                SET status = 'failed', response_json = ?,
                    error_code = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (encoded, code, now_text, now_text, selected_id),
            )
            cursor = self.connection.execute(
                """
                UPDATE management_idempotency_cache
                SET response_json = ?, status_code = ?, state = 'failed'
                WHERE actor_user_id = ? AND operation = ?
                  AND idempotency_key = ? AND payload_hash = ?
                  AND state = 'pending'
                """,
                (
                    encoded,
                    status_code,
                    int(row["actor_user_id"]),
                    str(row["operation"]),
                    str(row["idempotency_key"]),
                    str(row["payload_hash"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ManagementIdempotencyConflictError(
                    "settings idempotency failure conflict"
                )
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=int(row["actor_user_id"]),
                actor_username=str(row["actor_username"]),
                event_type="settings_update_failed",
                target_type="settings",
                target_id="selector_probe",
                result="failed",
                details={
                    "intent_id": selected_id,
                    "staged_revision": int(row["staged_revision"]),
                    "error_code": code,
                },
                created_at=now_text,
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def cached_management_response(
        self,
        *,
        actor_user_id: int,
        operation: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> tuple[dict[str, object], int] | None:
        actor = _positive_integer(actor_user_id, "actor_user_id")
        selected_operation = _gate_text(operation, "operation")
        key = _gate_text(idempotency_key, "idempotency_key")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        row = self.connection.execute(
            """
            SELECT response_json, status_code
            FROM management_idempotency_cache
            WHERE actor_user_id = ?
              AND operation = ?
              AND idempotency_key = ?
              AND expires_at > ?
            """,
            (actor, selected_operation, key, timestamp),
        ).fetchone()
        if row is None:
            return None
        return (
            _decode_json_object(
                row["response_json"],
                "management cached response",
            ),
            int(row["status_code"]),
        )

    def reserve_management_operation(
        self,
        *,
        actor_user_id: int,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
        request_payload: Mapping[str, object],
        pending_response: Mapping[str, object],
        pending_status_code: int = 202,
        now: datetime | None = None,
    ) -> dict[str, object]:
        actor = _positive_integer(actor_user_id, "actor_user_id")
        selected_operation = _gate_text(operation, "operation")
        key = _gate_text(idempotency_key, "idempotency_key")
        digest = _required_text(payload_hash, "payload_hash")
        selected_now = (now or datetime.now(UTC)).astimezone(UTC)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                DELETE FROM management_idempotency_cache
                WHERE expires_at <= ?
                """,
                (selected_now.isoformat(),),
            )
            row = self.connection.execute(
                """
                SELECT payload_hash, state, response_json, status_code,
                       created_at
                FROM management_idempotency_cache
                WHERE actor_user_id = ? AND operation = ?
                  AND idempotency_key = ?
                """,
                (actor, selected_operation, key),
            ).fetchone()
            if row is not None:
                stored_hash = str(row["payload_hash"] or "")
                if stored_hash and stored_hash != digest:
                    raise ManagementIdempotencyConflictError(
                        "idempotency key payload conflict"
                    )
                state = str(row["state"])
                if state == "pending" and stored_hash:
                    created_at = datetime.fromisoformat(
                        _iso_timestamp(
                            row["created_at"],
                            "management operation created_at",
                        )
                    )
                    lease_expired = (
                        created_at
                        + timedelta(
                            seconds=MANAGEMENT_PENDING_LEASE_SECONDS
                        )
                        <= selected_now
                    )
                    if lease_expired:
                        pending_json = _json(dict(pending_response))
                        cursor = self.connection.execute(
                            """
                            UPDATE management_idempotency_cache
                            SET response_json = ?, status_code = ?,
                                request_json = '{}', expires_at = ?,
                                created_at = ?
                            WHERE actor_user_id = ? AND operation = ?
                              AND idempotency_key = ?
                              AND payload_hash = ?
                              AND state = 'pending'
                              AND created_at = ?
                            """,
                            (
                                pending_json,
                                pending_status_code,
                                (
                                    selected_now + timedelta(hours=24)
                                ).isoformat(),
                                selected_now.isoformat(),
                                actor,
                                selected_operation,
                                key,
                                digest,
                                row["created_at"],
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ManagementIdempotencyConflictError(
                                "idempotency lease takeover conflict"
                            )
                        self.connection.commit()
                        return {
                            "reserved": True,
                            "state": "pending",
                            "response": dict(pending_response),
                            "status_code": pending_status_code,
                        }
                self.connection.commit()
                return {
                    "reserved": False,
                    "state": state,
                    "response": _decode_json_object(
                        row["response_json"],
                        "management idempotency response",
                    ),
                    "status_code": int(row["status_code"]),
                }
            self.connection.execute(
                """
                INSERT INTO management_idempotency_cache (
                    actor_user_id, operation, idempotency_key,
                    response_json, status_code, payload_hash, state,
                    request_json, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    actor,
                    selected_operation,
                    key,
                    _json(dict(pending_response)),
                    pending_status_code,
                    digest,
                    "{}",
                    (selected_now + timedelta(hours=24)).isoformat(),
                    selected_now.isoformat(),
                ),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return {
            "reserved": True,
            "state": "pending",
            "response": dict(pending_response),
            "status_code": pending_status_code,
        }

    def complete_management_operation(
        self,
        *,
        actor_user_id: int,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
        response: Mapping[str, object],
        status_code: int,
        failed: bool = False,
    ) -> None:
        actor = _positive_integer(actor_user_id, "actor_user_id")
        selected_operation = _gate_text(operation, "operation")
        key = _gate_text(idempotency_key, "idempotency_key")
        digest = _required_text(payload_hash, "payload_hash")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE management_idempotency_cache
                SET response_json = ?, status_code = ?, state = ?
                WHERE actor_user_id = ? AND operation = ?
                  AND idempotency_key = ? AND payload_hash = ?
                  AND state = 'pending'
                """,
                (
                    _json(dict(response)),
                    status_code,
                    "failed" if failed else "completed",
                    actor,
                    selected_operation,
                    key,
                    digest,
                ),
            )
            if cursor.rowcount != 1:
                row = self.connection.execute(
                    """
                    SELECT payload_hash FROM management_idempotency_cache
                    WHERE actor_user_id = ? AND operation = ?
                      AND idempotency_key = ?
                    """,
                    (actor, selected_operation, key),
                ).fetchone()
                if row is None or str(row["payload_hash"]) != digest:
                    raise ManagementIdempotencyConflictError(
                        "idempotency completion conflict"
                    )

    def cache_management_response(
        self,
        *,
        actor_user_id: int,
        operation: str,
        idempotency_key: str,
        response: Mapping[str, object],
        status_code: int,
        now: datetime | None = None,
    ) -> None:
        actor = _positive_integer(actor_user_id, "actor_user_id")
        selected_operation = _gate_text(operation, "operation")
        key = _gate_text(idempotency_key, "idempotency_key")
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 200 <= status_code <= 599
        ):
            raise ValueError("status_code is invalid")
        selected_now = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM management_idempotency_cache
                WHERE expires_at <= ?
                """,
                (selected_now.isoformat(),),
            )
            self.connection.execute(
                """
                INSERT INTO management_idempotency_cache (
                    actor_user_id, operation, idempotency_key,
                    response_json, status_code, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_user_id, operation, idempotency_key)
                DO NOTHING
                """,
                (
                    actor,
                    selected_operation,
                    key,
                    _json(dict(response)),
                    status_code,
                    (selected_now + timedelta(hours=24)).isoformat(),
                    selected_now.isoformat(),
                ),
            )

    def record_management_audit(
        self,
        *,
        actor_user_id: int,
        actor_username: str,
        event_type: str,
        target_type: str,
        target_id: str,
        details: Mapping[str, object],
        result: str = "succeeded",
    ) -> None:
        actor_id, actor_name = _selector_actor(
            actor_user_id, actor_username
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO selector_management_audit_events (
                    actor_user_id, actor_username, event_type, target_type,
                    target_id, result, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    actor_name,
                    _gate_text(event_type, "event_type"),
                    _gate_text(target_type, "target_type"),
                    _gate_text(target_id, "target_id"),
                    _gate_text(result, "result"),
                    _json(dict(details)),
                    _utc_now(),
                ),
            )

    def save_management_preflight_health(
        self,
        workspace: str,
        result: Mapping[str, object],
        *,
        checked_at: str,
    ) -> None:
        selected = _gate_text(workspace, "workspace")
        timestamp = _iso_timestamp(checked_at, "checked_at")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO management_preflight_health (
                    workspace, result_json, checked_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(workspace) DO UPDATE SET
                    result_json = excluded.result_json,
                    checked_at = excluded.checked_at
                """,
                (selected, _json(dict(result)), timestamp),
            )

    def management_preflight_health(
        self,
        workspace: str,
    ) -> dict[str, object] | None:
        selected = _gate_text(workspace, "workspace")
        row = self.connection.execute(
            """
            SELECT result_json, checked_at
            FROM management_preflight_health
            WHERE workspace = ?
            """,
            (selected,),
        ).fetchone()
        if row is None:
            return None
        result = _decode_json_object(
            row["result_json"], "management preflight health"
        )
        result["checked_at"] = str(row["checked_at"])
        return result

    def list_management_rows(
        self,
        resource: str,
        *,
        page: int,
        page_size: int,
        filters: Mapping[str, str] | None = None,
    ) -> tuple[list[dict[str, object]], int, int]:
        selected = _gate_text(resource, "resource")
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or page_size not in {20, 50, 100}
        ):
            raise ValueError("invalid_pagination")
        selected_filters = dict(filters or {})
        offset = (page - 1) * page_size
        if selected == "runs":
            status = selected_filters.get("status", "all")
            records: list[dict[str, object]] = []
            linked = self.connection.execute(
                """
                SELECT m.*, p.scheduled_for, p.started_at,
                       p.active_version_before,
                       p.published_version_after,
                       p.failed_aliases_json, p.details_json
                FROM management_run_requests AS m
                LEFT JOIN probe_runs AS p ON p.id = m.probe_run_id
                ORDER BY m.created_at DESC
                """
            ).fetchall()
            for row in linked:
                details = (
                    _decode_json_object(
                        row["details_json"], "probe run details"
                    )
                    if row["details_json"] is not None
                    else {}
                )
                details.update(
                    {
                        "trigger": str(row["trigger"]),
                        "actor": str(row["actor_username"]),
                        "retry_of_run_id": str(row["retry_of_run_id"]),
                    }
                )
                records.append(
                    {
                        "id": str(row["id"]),
                        "request_id": str(row["id"]),
                        "probe_run_id": row["probe_run_id"],
                        "scheduled_for": str(
                            row["scheduled_for"] or row["created_at"]
                        ),
                        "started_at": str(
                            row["started_at"] or row["created_at"]
                        ),
                        "finished_at": row["finished_at"],
                        "status": str(row["status"]),
                        "active_version_before": str(
                            row["active_version_before"] or ""
                        ),
                        "published_version_after": str(
                            row["published_version_after"] or ""
                        ),
                        "failed_aliases_json": str(
                            row["failed_aliases_json"] or "[]"
                        ),
                        "details_json": _json(details),
                        "failure_code": str(row["failure_code"]),
                    }
                )
            unlinked = self.connection.execute(
                """
                SELECT p.*
                FROM probe_runs AS p
                WHERE NOT EXISTS (
                    SELECT 1 FROM management_run_requests AS m
                    WHERE m.probe_run_id = p.id
                )
                ORDER BY p.id DESC
                """
            ).fetchall()
            for row in unlinked:
                item = dict(row)
                details = _decode_json_object(
                    item["details_json"], "probe run details"
                )
                details.setdefault("trigger", "scheduled")
                details.setdefault("actor", "system")
                item["details_json"] = _json(details)
                item["request_id"] = ""
                item["probe_run_id"] = int(row["id"])
                records.append(item)
            if status != "all":
                records = [
                    item for item in records if item["status"] == status
                ]
            records.sort(
                key=lambda item: str(
                    item.get("started_at")
                    or item.get("scheduled_for")
                    or ""
                ),
                reverse=True,
            )
            return (
                records[offset : offset + page_size],
                len(records),
                self.current_revision("runs"),
            )
        if selected in {"runs", "versions", "alerts", "audit"}:
            table, order = {
                "runs": ("probe_runs", "id DESC"),
                "versions": (
                    "selector_versions",
                    "created_at DESC, id DESC",
                ),
                "alerts": ("probe_alerts", "last_seen_at DESC, id DESC"),
                "audit": (
                    "selector_management_audit_events",
                    "created_at DESC, id DESC",
                ),
            }[selected]
            allowed_filter = {
                "runs": "status",
                "versions": "status",
                "alerts": "status",
                "audit": "event_type",
            }[selected]
            clauses: list[str] = []
            arguments: list[object] = []
            filter_value = selected_filters.get(allowed_filter, "")
            if filter_value and filter_value != "all":
                clauses.append(f"{allowed_filter} = ?")
                arguments.append(_gate_text(filter_value, allowed_filter))
            if selected == "alerts":
                failure = selected_filters.get("failure_class", "")
                if failure:
                    clauses.append("failure_class = ?")
                    arguments.append(
                        _gate_text(failure, "failure_class")
                    )
            if selected == "audit":
                target_id = selected_filters.get("target_id", "")
                if target_id:
                    clauses.append("target_id = ?")
                    arguments.append(_gate_text(target_id, "target_id"))
            where = " AND ".join(clauses) or "1 = 1"
            total = int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}",
                    arguments,
                ).fetchone()[0]
            )
            rows = self.connection.execute(
                f"""
                SELECT *
                FROM {table}
                WHERE {where}
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """,
                (*arguments, page_size, offset),
            ).fetchall()
            projected_rows = [dict(row) for row in rows]
            if selected == "alerts":
                for item in projected_rows:
                    strategy_ids = _decode_string_array(
                        item["strategy_ids_json"], "strategy IDs"
                    )
                    if not strategy_ids:
                        item["gate_active"] = False
                        continue
                    placeholders = ",".join("?" for _ in strategy_ids)
                    item["gate_active"] = (
                        self.connection.execute(
                            f"""
                            SELECT 1 FROM strategy_gate_reasons
                            WHERE strategy_id IN ({placeholders})
                              AND cleared_at IS NULL
                            LIMIT 1
                            """,
                            strategy_ids,
                        ).fetchone()
                        is not None
                    )
            return (
                projected_rows,
                total,
                self.current_revision(selected),
            )
        if selected != "gates":
            raise ValueError("unsupported resource")
        strategy_rows = self.connection.execute(
            """
            SELECT strategy_id FROM strategy_dependencies
            UNION
            SELECT strategy_id FROM strategy_gate_revisions
            UNION
            SELECT strategy_id FROM strategy_gate_reasons
            ORDER BY strategy_id
            """
        ).fetchall()
        records: list[dict[str, object]] = []
        for strategy_row in strategy_rows:
            strategy_id = str(strategy_row["strategy_id"])
            revision, managed, reasons = self.gate_snapshot(strategy_id)
            reason_values = [
                {
                    **dict(row),
                    "aliases": json.loads(row["aliases_json"]),
                }
                for row in reasons
            ]
            dependencies = self.connection.execute(
                """
                SELECT action_id, action_type, strategy_name, alias
                FROM strategy_dependencies
                WHERE strategy_id = ?
                ORDER BY action_id, alias
                """,
                (strategy_id,),
            ).fetchall()
            record = {
                "strategy_id": strategy_id,
                "strategy_name": (
                    str(dependencies[0]["strategy_name"])
                    if dependencies
                    else ""
                ),
                "managed": managed,
                "allowed": not reason_values,
                "effective_status": (
                    "active"
                    if managed and not reason_values
                    else "paused"
                    if reason_values
                    else "unmanaged"
                ),
                "revision": revision,
                "reasons": reason_values,
                "affected_action_ids": sorted(
                    {str(row["action_id"]) for row in dependencies}
                ),
            }
            status = selected_filters.get("status", "all")
            source = selected_filters.get("source", "all")
            search = selected_filters.get("search", "").casefold()
            if status != "all" and record["effective_status"] != status:
                continue
            if source != "all" and not any(
                item["source"] == source for item in reason_values
            ):
                continue
            if search and search not in (
                strategy_id + " " + str(record["strategy_name"])
            ).casefold():
                continue
            records.append(record)
        total = len(records)
        return (
            records[offset : offset + page_size],
            total,
            self.current_revision("gates"),
        )

    def management_run_detail(
        self,
        run_id: object,
    ) -> dict[str, object] | None:
        if isinstance(run_id, bool) or not isinstance(run_id, (int, str)):
            raise ValueError("run_id is invalid")
        selected_text = str(run_id).strip()
        if not selected_text or len(selected_text) > 128:
            raise ValueError("run_id is invalid")
        request_row = self.connection.execute(
            """
            SELECT m.*, p.scheduled_for, p.started_at,
                   p.active_version_before, p.published_version_after,
                   p.failed_aliases_json, p.details_json
            FROM management_run_requests AS m
            LEFT JOIN probe_runs AS p ON p.id = m.probe_run_id
            WHERE m.id = ?
            """,
            (selected_text,),
        ).fetchone()
        if request_row is not None:
            details = (
                _decode_json_object(
                    request_row["details_json"], "probe run details"
                )
                if request_row["details_json"] is not None
                else {}
            )
            details.update(
                {
                    "trigger": str(request_row["trigger"]),
                    "actor": str(request_row["actor_username"]),
                    "retry_of_run_id": str(
                        request_row["retry_of_run_id"]
                    ),
                }
            )
            probe_run_id = request_row["probe_run_id"]
            result = {
                "id": str(request_row["id"]),
                "request_id": str(request_row["id"]),
                "probe_run_id": probe_run_id,
                "status": str(request_row["status"]),
                "scheduled_for": str(
                    request_row["scheduled_for"]
                    or request_row["created_at"]
                ),
                "started_at": str(
                    request_row["started_at"]
                    or request_row["created_at"]
                ),
                "finished_at": request_row["finished_at"],
                "active_version_before": str(
                    request_row["active_version_before"] or ""
                ),
                "published_version_after": str(
                    request_row["published_version_after"] or ""
                ),
                "failed_aliases": json.loads(
                    request_row["failed_aliases_json"] or "[]"
                ),
                "details": details,
                "failure_code": str(request_row["failure_code"]),
            }
            if probe_run_id is None:
                result["validations"] = []
            else:
                result["validations"] = [
                    dict(item)
                    for item in self.connection.execute(
                        """
                        SELECT profile_mask, round_number, page_state,
                               result, failure_code, evidence_json,
                               screenshot_path, started_at, finished_at
                        FROM selector_validation_runs
                        WHERE probe_run_id = ?
                        ORDER BY round_number, profile_mask, id
                        """,
                        (probe_run_id,),
                    ).fetchall()
                ]
            return result
        if not selected_text.isdigit():
            return None
        selected = _positive_integer(int(selected_text), "run_id")
        row = self.connection.execute(
            "SELECT * FROM probe_runs WHERE id = ?",
            (selected,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = _decode_json_object(
            result.pop("details_json"),
            "probe run details",
        )
        result["failed_aliases"] = json.loads(
            result.pop("failed_aliases_json")
        )
        validations = self.connection.execute(
            """
            SELECT profile_mask, round_number, page_state, result,
                   failure_code, evidence_json, screenshot_path,
                   started_at, finished_at
            FROM selector_validation_runs
            WHERE probe_run_id = ?
            ORDER BY round_number, profile_mask, id
            """,
            (selected,),
        ).fetchall()
        result["validations"] = [dict(item) for item in validations]
        return result

    def create_management_run_request(
        self,
        request_id: str,
        *,
        actor_user_id: int,
        actor_username: str,
        retry_of_run_id: object = "",
    ) -> dict[str, object]:
        selected = _gate_text(request_id, "request_id")
        actor_id, actor_name = _selector_actor(
            actor_user_id, actor_username
        )
        retry = str(retry_of_run_id or "").strip()
        if len(retry) > 128:
            raise ValueError("retry_of_run_id is invalid")
        self.expire_stale_management_run_requests()
        active = self.active_management_run_request()
        if active is not None:
            active["deduplicated"] = True
            return active
        now = _utc_now()
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO management_run_requests (
                        id, actor_user_id, actor_username, trigger,
                        retry_of_run_id, probe_run_id, status,
                        failure_code, created_at, updated_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, 'queued', '', ?, ?, NULL)
                    """,
                    (
                        selected,
                        actor_id,
                        actor_name,
                        "retry" if retry else "manual",
                        retry,
                        now,
                        now,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO management_resource_revisions(
                        resource, revision
                    ) VALUES ('runs', 1)
                    ON CONFLICT(resource) DO UPDATE
                    SET revision = revision + 1
                    """
                )
        except sqlite3.IntegrityError:
            active = self.active_management_run_request()
            if active is None:
                raise
            active["deduplicated"] = True
            return active
        return {
            "id": selected,
            "status": "queued",
            "retry_of_run_id": retry,
            "created_at": now,
            "deduplicated": False,
        }

    def active_management_run_request(self) -> dict[str, object] | None:
        row = self.connection.execute(
            """
            SELECT id, status, trigger, retry_of_run_id, probe_run_id,
                   created_at, updated_at
            FROM management_run_requests
            WHERE status IN ('queued', 'running')
            ORDER BY created_at
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def expire_stale_management_run_requests(
        self,
        *,
        stale_after: timedelta = timedelta(minutes=30),
    ) -> int:
        if not isinstance(stale_after, timedelta) or stale_after <= timedelta():
            raise ValueError("stale_after must be positive")
        cutoff = (datetime.now(UTC) - stale_after).isoformat()
        now = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE management_run_requests
                SET status = 'failed', failure_code = 'stale_request',
                    updated_at = ?, finished_at = ?
                WHERE status IN ('queued', 'running') AND updated_at < ?
                """,
                (now, now, cutoff),
            )
            if cursor.rowcount:
                self.connection.execute(
                    """
                    INSERT INTO management_resource_revisions(
                        resource, revision
                    ) VALUES ('runs', 1)
                    ON CONFLICT(resource) DO UPDATE
                    SET revision = revision + 1
                    """
                )
        return int(cursor.rowcount)

    def fail_management_run_request(
        self,
        request_id: str,
        failure_code: str = "dispatch_failed",
    ) -> None:
        selected = _gate_text(request_id, "request_id")
        safe_failure = _gate_text(failure_code, "failure_code")[:128]
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE management_run_requests
                SET status = 'dispatch_failed', failure_code = ?,
                    updated_at = ?, finished_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (safe_failure, now, now, selected),
            )
            self.connection.execute(
                """
                INSERT INTO management_resource_revisions(resource, revision)
                VALUES ('runs', 1)
                ON CONFLICT(resource) DO UPDATE
                SET revision = revision + 1
                """
            )

    def finish_management_run_request(
        self,
        request_id: str,
        *,
        status: str,
        failure_code: str = "",
    ) -> None:
        selected = _gate_text(request_id, "request_id")
        terminal = _gate_text(status, "status")
        if terminal not in {"completed", "failed", "dispatch_failed"}:
            raise ValueError("management run status is not terminal")
        safe_failure = str(failure_code or "").strip()[:128]
        if terminal != "completed" and not safe_failure:
            safe_failure = "probe_failed"
        now = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE management_run_requests
                SET status = ?, failure_code = ?, updated_at = ?,
                    finished_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (terminal, safe_failure, now, now, selected),
            )
            if cursor.rowcount:
                self.connection.execute(
                    """
                    INSERT INTO management_resource_revisions(
                        resource, revision
                    ) VALUES ('runs', 1)
                    ON CONFLICT(resource) DO UPDATE
                    SET revision = revision + 1
                    """
                )

    def update_run_progress(
        self,
        run_id: int,
        *,
        attempt_token: str = "",
        stages: Sequence[Mapping[str, object]],
    ) -> None:
        selected = _positive_integer(run_id, "run_id")
        token = _required_text_or_empty(attempt_token, "attempt_token")
        if not isinstance(stages, Sequence) or isinstance(
            stages, (str, bytes, bytearray)
        ):
            raise ValueError("stages must be a sequence")
        row = self.connection.execute(
            """
            SELECT details_json FROM probe_runs
            WHERE id = ? AND (? = '' OR attempt_token = ?)
            """,
            (selected, token, token),
        ).fetchone()
        if row is None:
            raise ValueError("probe run does not exist")
        details = _decode_json_object(
            row["details_json"], "probe run details"
        )
        details["stages"] = [dict(item) for item in stages]
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE probe_runs SET details_json = ? WHERE id = ?",
                (_json(details), selected),
            )
            self.connection.execute(
                """
                UPDATE management_run_requests
                SET updated_at = ?
                WHERE probe_run_id = ? AND status = 'running'
                """,
                (now, selected),
            )

    def management_version_detail(
        self,
        version_id: str,
    ) -> dict[str, object] | None:
        version = self.get_version(version_id)
        if version is None:
            return None
        outbox = self.connection.execute(
            """
            SELECT status, attempt_count, next_attempt_at, last_error,
                   created_at, completed_at
            FROM publication_outbox
            WHERE aggregate_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (version_id,),
        ).fetchone()
        version["outbox"] = dict(outbox) if outbox is not None else {}
        return version

    def management_version_diff(
        self,
        version_id: str,
    ) -> dict[str, object] | None:
        current = self.get_version(version_id)
        if current is None:
            return None
        base_id = str(current.get("base_version_id") or "")
        base = self.get_version(base_id) if base_id else None
        current_elements = current["bundle"].get("elements", {})
        base_elements = (
            base["bundle"].get("elements", {})
            if isinstance(base, Mapping)
            else {}
        )
        changed = []
        for alias in sorted(set(current_elements) | set(base_elements)):
            before = base_elements.get(alias)
            after = current_elements.get(alias)
            if before == after:
                continue
            changed.append(
                {
                    "alias": alias,
                    "change": (
                        "added"
                        if before is None
                        else "removed"
                        if after is None
                        else "changed"
                    ),
                    "from_version": base_id,
                    "to_version": version_id,
                }
            )
        return {
            "version_id": version_id,
            "base_version_id": base_id,
            "changed_elements": changed,
        }

    def request_rollback_validation(
        self,
        version_id: str,
        *,
        actor_user_id: int,
        actor_username: str,
        reason: str,
    ) -> dict[str, object]:
        source_id = _required_text(version_id, "version_id")
        selected_reason = _required_text(reason, "reason")
        actor_id, actor_name = _selector_actor(
            actor_user_id,
            actor_username,
        )
        now = datetime.now(UTC)
        draft_id = (
            f"rollback-{now.strftime('%Y%m%d-%H%M%S')}-"
            f"{secrets.token_hex(4)}"
        )
        timestamp = now.isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            source = self.connection.execute(
                """
                SELECT *
                FROM selector_versions
                WHERE id = ? AND status IN ('published', 'superseded')
                """,
                (source_id,),
            ).fetchone()
            if source is None:
                raise KeyError("version not found")
            bundle = _decode_json_object(
                source["bundle_json"],
                "selector version bundle",
            )
            self.connection.execute(
                """
                INSERT INTO selector_versions (
                    id, site, environment, status, base_version_id,
                    bundle_json, bundle_hash, evidence_json, model_id,
                    prompt_version, created_at, validated_at
                ) VALUES (?, ?, ?, 'rollback_draft', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    source["site"],
                    source["environment"],
                    source_id,
                    _canonical_json(bundle),
                    source["bundle_hash"],
                    source["evidence_json"],
                    source["model_id"],
                    source["prompt_version"],
                    timestamp,
                    timestamp,
                ),
            )
            _insert_selector_management_audit(
                self.connection,
                actor_user_id=actor_id,
                actor_username=actor_name,
                event_type="rollback_validation_requested",
                target_type="selector_version",
                target_id=source_id,
                details={
                    "draft_version": draft_id,
                    "reason": selected_reason[:500],
                },
                created_at=timestamp,
            )
            self.connection.execute(
                """
                INSERT INTO management_resource_revisions(resource, revision)
                VALUES ('versions', 1)
                ON CONFLICT(resource) DO UPDATE SET revision = revision + 1
                """
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return {
            "status": "accepted",
            "source_version": source_id,
            "draft_version": draft_id,
            "request_id": draft_id,
        }

    def replace_strategy_dependencies(
        self,
        dependencies: object,
    ) -> tuple[str, ...]:
        rows = _validated_dependency_rows(dependencies)
        strategy_ids = tuple(sorted({row[1] for row in rows}))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            previous_dependency_rows = self.connection.execute(
                """
                SELECT alias, strategy_id, action_id, action_type, strategy_name
                FROM strategy_dependencies
                ORDER BY alias, strategy_id, action_id
                """
            ).fetchall()
            dependencies_changed = [
                tuple(row) for row in previous_dependency_rows
            ] != rows
            previous_rows = self.connection.execute(
                "SELECT DISTINCT strategy_id FROM strategy_dependencies"
            ).fetchall()
            previous = {
                str(row["strategy_id"]) for row in previous_rows
            }
            self.connection.execute("DELETE FROM strategy_dependencies")
            self.connection.executemany(
                """
                INSERT INTO strategy_dependencies (
                    alias,
                    strategy_id,
                    action_id,
                    action_type,
                    strategy_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            if dependencies_changed:
                self.connection.execute(
                    """
                    UPDATE element_catalog_state
                    SET revision = revision + 1
                    WHERE singleton = 1
                    """
                )
            _bump_gate_revisions(
                self.connection,
                previous | set(strategy_ids),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return strategy_ids

    def dependency_rows_for_aliases(
        self,
        aliases: object,
    ) -> tuple[sqlite3.Row, ...]:
        values = _gate_text_sequence(aliases, "aliases")
        if not values:
            return ()
        placeholders = ",".join("?" for _item in values)
        rows = self.connection.execute(
            f"""
            SELECT alias, strategy_id, action_id, action_type, strategy_name
            FROM strategy_dependencies
            WHERE alias IN ({placeholders})
            ORDER BY alias, strategy_id, action_id
            """,
            values,
        ).fetchall()
        return tuple(rows)

    def managed_strategy_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT strategy_id
            FROM strategy_dependencies
            ORDER BY strategy_id
            """
        ).fetchall()
        return tuple(str(row["strategy_id"]) for row in rows)

    def upsert_gate_reasons(self, reasons: object) -> tuple[str, ...]:
        prepared = _validated_gate_reasons(reasons)
        if not prepared:
            return ()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            changed: set[str] = set()
            for (
                strategy_id,
                source,
                site,
                environment,
                reason_code,
                aliases,
                selector_version_id,
                created_by,
            ) in prepared:
                row = self.connection.execute(
                    """
                    SELECT id, aliases_json
                    FROM strategy_gate_reasons
                    WHERE strategy_id = ?
                      AND source = ?
                      AND site = ?
                      AND environment = ?
                      AND reason_code = ?
                      AND selector_version_id = ?
                      AND cleared_at IS NULL
                    """,
                    (
                        strategy_id,
                        source,
                        site,
                        environment,
                        reason_code,
                        selector_version_id,
                    ),
                ).fetchone()
                if row is None:
                    self.connection.execute(
                        """
                        INSERT INTO strategy_gate_reasons (
                            strategy_id,
                            source,
                            site,
                            environment,
                            reason_code,
                            aliases_json,
                            selector_version_id,
                            created_at,
                            created_by
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            strategy_id,
                            source,
                            site,
                            environment,
                            reason_code,
                            _json(aliases),
                            selector_version_id,
                            _utc_now(),
                            created_by,
                        ),
                    )
                    changed.add(strategy_id)
                    continue
                existing = _decode_string_array(
                    row["aliases_json"],
                    "gate aliases",
                )
                merged = sorted(set(existing) | set(aliases))
                if merged != existing:
                    self.connection.execute(
                        """
                        UPDATE strategy_gate_reasons
                        SET aliases_json = ?
                        WHERE id = ? AND cleared_at IS NULL
                        """,
                        (_json(merged), row["id"]),
                    )
                    changed.add(strategy_id)
            _bump_gate_revisions(self.connection, changed)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return tuple(sorted({item[0] for item in prepared}))

    def clear_gate_reasons(
        self,
        strategy_ids: object,
        *,
        source: str,
        cleared_by: str,
        site: str | None = None,
        environment: str | None = None,
    ) -> tuple[str, ...]:
        strategies = _gate_text_sequence(strategy_ids, "strategy_ids")
        if source not in {"probe", "manual"}:
            raise ValueError("gate source must be probe or manual")
        actor = _gate_text(cleared_by, "cleared_by")
        scope_sql = ""
        scope_args: tuple[str, ...] = ()
        if source == "probe":
            scope_args = (
                _key_segment(site, "site"),
                _key_segment(environment, "environment"),
            )
            scope_sql = " AND site = ? AND environment = ?"
        if not strategies:
            return ()
        placeholders = ",".join("?" for _item in strategies)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                f"""
                SELECT DISTINCT strategy_id
                FROM strategy_gate_reasons
                WHERE strategy_id IN ({placeholders})
                  AND source = ?
                  {scope_sql}
                  AND cleared_at IS NULL
                ORDER BY strategy_id
                """,
                (*strategies, source, *scope_args),
            ).fetchall()
            affected = tuple(str(row["strategy_id"]) for row in rows)
            if affected:
                affected_placeholders = ",".join("?" for _item in affected)
                self.connection.execute(
                    f"""
                    UPDATE strategy_gate_reasons
                    SET cleared_at = ?, cleared_by = ?
                    WHERE strategy_id IN ({affected_placeholders})
                      AND source = ?
                      {scope_sql}
                      AND cleared_at IS NULL
                    """,
                    (
                        _utc_now(),
                        actor,
                        *affected,
                        source,
                        *scope_args,
                    ),
                )
                _bump_gate_revisions(self.connection, set(affected))
            self.connection.commit()
            return affected
        except BaseException:
            self.connection.rollback()
            raise

    def open_gate_reason_rows(
        self,
        strategy_id: str,
        *,
        site: str = "tiktok",
        environment: str = "production",
    ) -> tuple[sqlite3.Row, ...]:
        strategy = _gate_text(strategy_id, "strategy_id")
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        rows = self.connection.execute(
            """
            SELECT source, reason_code, aliases_json, selector_version_id,
                   created_at, created_by
            FROM strategy_gate_reasons
            WHERE strategy_id = ?
              AND cleared_at IS NULL
              AND (
                  source = 'manual'
                  OR (source = 'probe' AND site = ? AND environment = ?)
              )
            ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END,
                     created_at,
                     id
            """,
            (strategy, site_value, environment_value),
        ).fetchall()
        return tuple(rows)

    def gate_snapshot(
        self,
        strategy_id: str,
        *,
        site: str = "tiktok",
        environment: str = "production",
    ) -> tuple[int, bool, tuple[sqlite3.Row, ...]]:
        strategy = _gate_text(strategy_id, "strategy_id")
        site_value = _key_segment(site, "site")
        environment_value = _key_segment(environment, "environment")
        self.connection.execute("BEGIN")
        try:
            revision_row = self.connection.execute(
                """
                SELECT revision
                FROM strategy_gate_revisions
                WHERE strategy_id = ?
                """,
                (strategy,),
            ).fetchone()
            managed = (
                self.connection.execute(
                    """
                    SELECT 1
                    FROM strategy_dependencies
                    WHERE strategy_id = ?
                    LIMIT 1
                    """,
                    (strategy,),
                ).fetchone()
                is not None
            )
            reasons = self.connection.execute(
                """
                SELECT source, reason_code, aliases_json,
                       selector_version_id, created_at, created_by
                FROM strategy_gate_reasons
                WHERE strategy_id = ?
                  AND cleared_at IS NULL
                  AND (
                      source = 'manual'
                      OR (
                          source = 'probe'
                          AND site = ?
                          AND environment = ?
                      )
                  )
                ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END,
                         created_at,
                         id
                """,
                (strategy, site_value, environment_value),
            ).fetchall()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return (
            int(revision_row["revision"]) if revision_row is not None else 0,
            managed,
            tuple(reasons),
        )

    def set_manual_gate_cas(
        self,
        strategy_id: str,
        *,
        paused: bool,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str,
        reason: str,
    ) -> dict[str, object]:
        strategy = _gate_text(strategy_id, "strategy_id")
        revision = _nonnegative_integer(
            expected_revision, "expected_revision"
        )
        actor_id, actor_name = _selector_actor(
            actor_user_id, actor_username
        )
        selected_reason = _required_text(reason, "reason")
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean")
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT revision FROM strategy_gate_revisions
                WHERE strategy_id = ?
                """,
                (strategy,),
            ).fetchone()
            current = int(row["revision"]) if row is not None else 0
            if current != revision:
                raise StaleManagementRevisionError("stale gate revision")
            if paused:
                existing = self.connection.execute(
                    """
                    SELECT id FROM strategy_gate_reasons
                    WHERE strategy_id = ? AND source = 'manual'
                      AND reason_code = 'operator_pause'
                      AND selector_version_id = ''
                      AND cleared_at IS NULL
                    """,
                    (strategy,),
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        """
                        INSERT INTO strategy_gate_reasons (
                            strategy_id, source, reason_code, aliases_json,
                            selector_version_id, created_at, created_by
                        ) VALUES (?, 'manual', 'operator_pause', '[]', '',
                                  ?, ?)
                        """,
                        (strategy, now, actor_name),
                    )
                    _bump_gate_revisions(self.connection, {strategy})
            else:
                cursor = self.connection.execute(
                    """
                    UPDATE strategy_gate_reasons
                    SET cleared_at = ?, cleared_by = ?
                    WHERE strategy_id = ? AND source = 'manual'
                      AND cleared_at IS NULL
                    """,
                    (now, actor_name, strategy),
                )
                if cursor.rowcount:
                    _bump_gate_revisions(self.connection, {strategy})
            self.connection.execute(
                """
                INSERT INTO selector_management_audit_events (
                    actor_user_id, actor_username, event_type, target_type,
                    target_id, result, details_json, created_at
                ) VALUES (?, ?, ?, 'strategy_gate', ?, 'succeeded', ?, ?)
                """,
                (
                    actor_id,
                    actor_name,
                    "manual_gate_paused" if paused else "manual_gate_resumed",
                    strategy,
                    _json({"reason": selected_reason[:500]}),
                    now,
                ),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        revision_value, managed, rows = self.gate_snapshot(strategy)
        return {
            "strategy_id": strategy,
            "revision": revision_value,
            "managed": managed,
            "reasons": [
                {
                    **dict(item),
                    "aliases": json.loads(item["aliases_json"]),
                }
                for item in rows
            ],
        }

    def transition_alert_cas(
        self,
        alert_id: int,
        *,
        status: str,
        expected_revision: int | None,
        actor_user_id: int,
        actor_username: str,
        reason: str,
    ) -> dict[str, object]:
        selected_id = _positive_integer(alert_id, "alert_id")
        actor_id, actor_name = _selector_actor(
            actor_user_id, actor_username
        )
        if status not in {"acknowledged", "resolved"}:
            raise ValueError("unsupported alert status")
        if status == "resolved":
            selected_reason = _required_text(reason, "reason")
        else:
            selected_reason = str(reason or "")
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM probe_alerts WHERE id = ?",
                (selected_id,),
            ).fetchone()
            if row is None:
                raise KeyError("alert not found")
            if expected_revision is not None and int(row["revision"]) != (
                _nonnegative_integer(expected_revision, "expected_revision")
            ):
                raise StaleManagementRevisionError(
                    "stale alert revision"
                )
            if (
                row["status"] == "resolved"
                and status == "acknowledged"
            ):
                raise ValueError(
                    "resolved alert cannot be acknowledged"
                )
            if status == "resolved":
                strategy_ids = _decode_string_array(
                    row["strategy_ids_json"], "strategy IDs"
                )
                if strategy_ids:
                    placeholders = ",".join("?" for _ in strategy_ids)
                    active = self.connection.execute(
                        f"""
                        SELECT 1 FROM strategy_gate_reasons
                        WHERE strategy_id IN ({placeholders})
                          AND cleared_at IS NULL
                          AND (
                              source = 'manual'
                              OR (
                                  source = 'probe'
                                  AND site = ?
                                  AND environment = ?
                              )
                          )
                        LIMIT 1
                        """,
                        (
                            *strategy_ids,
                            str(row["site"]),
                            str(row["environment"]),
                        ),
                    ).fetchone()
                    if active is not None:
                        raise GateStillActiveError("gate remains active")
            timestamp_column = (
                "acknowledged_at"
                if status == "acknowledged"
                else "resolved_at"
            )
            if row["status"] != status:
                self.connection.execute(
                    f"""
                    UPDATE probe_alerts
                    SET status = ?, {timestamp_column} = ?,
                        revision = revision + 1
                    WHERE id = ?
                    """,
                    (status, now, selected_id),
                )
                transitioned = self.connection.execute(
                    "SELECT * FROM probe_alerts WHERE id = ?",
                    (selected_id,),
                ).fetchone()
                self.connection.execute(
                    """
                    INSERT INTO webhook_outbox (
                        alert_id, event_type, payload_json, status,
                        next_attempt_at, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        selected_id,
                        f"alert_{status}",
                        _alert_webhook_payload(
                            transitioned,
                            event_type=f"alert_{status}",
                        ),
                        now,
                        now,
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO selector_management_audit_events (
                    actor_user_id, actor_username, event_type, target_type,
                    target_id, result, details_json, created_at
                ) VALUES (?, ?, ?, 'alert', ?, 'succeeded', ?, ?)
                """,
                (
                    actor_id,
                    actor_name,
                    f"alert_{status}",
                    str(selected_id),
                    _json({"reason": selected_reason[:500]}),
                    now,
                ),
            )
            current = self.connection.execute(
                "SELECT * FROM probe_alerts WHERE id = ?",
                (selected_id,),
            ).fetchone()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return _alert_record(current)


def _required_text_or_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{name} must be a trimmed string")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _selector_actor(
    actor_user_id: object,
    actor_username: object,
) -> tuple[int, str]:
    return (
        _positive_integer(actor_user_id, "actor_user_id"),
        _gate_text(actor_username, "actor_username"),
    )


def _string_sequence(value: object, name: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array of strings")
    return [_required_text(item, name) for item in value]


def _prepare_contracts(
    contracts: object,
) -> list[tuple[str, str, str, str, bool]]:
    if not isinstance(contracts, Mapping):
        raise ValueError("contracts must be a JSON object")

    prepared: list[tuple[str, str, str, str, bool]] = []
    for alias, contract in contracts.items():
        alias_text = _required_text(alias, "contract alias")
        if not isinstance(contract, Mapping):
            raise ValueError(f"contract for {alias_text!r} must be a JSON object")
        contract_value = dict(contract)
        site = _required_text(
            contract_value.get("site", "tiktok"),
            f"contract {alias_text!r} site",
        )
        environment = _required_text(
            contract_value.get("environment", "production"),
            f"contract {alias_text!r} environment",
        )
        enabled = contract_value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"contract {alias_text!r} enabled must be a boolean")
        contract_json = _json(contract_value)
        prepared.append(
            (alias_text, site, environment, contract_json, enabled)
        )
    return prepared


def _key_segment(value: object, name: str) -> str:
    text = _required_text(value, name)
    if not _KEY_SEGMENT.fullmatch(text):
        raise ValueError(f"{name} must be a safe key segment")
    return text


def _validated_bundle(value: object) -> tuple[dict[str, object], str]:
    if not isinstance(value, Mapping) or set(value) not in (
        {"elements"},
        {"bundle_hash", "elements"},
    ):
        raise ValueError("bundle has an invalid shape")
    raw_elements = value.get("elements")
    if not isinstance(raw_elements, dict) or not raw_elements:
        raise ValueError("bundle elements must be a non-empty object")
    try:
        elements = normalize_element_definitions(raw_elements)
    except (TypeError, ValueError) as error:
        raise ValueError("bundle elements are invalid") from error
    if elements != raw_elements:
        raise ValueError("bundle elements must be canonical")
    if len(elements) > 256 or any(
        len(definition["locators"]) > 5 for definition in elements.values()
    ):
        raise ValueError("bundle exceeds resource budget")
    bundle_hash = _sha256(elements)
    supplied_hash = value.get("bundle_hash")
    if supplied_hash is not None and supplied_hash != bundle_hash:
        raise ValueError("bundle_hash does not match canonical elements")
    canonical = {"bundle_hash": bundle_hash, "elements": elements}
    if len(_canonical_json(canonical).encode("utf-8")) > 262_144:
        raise ValueError("bundle exceeds resource budget")
    return canonical, bundle_hash


def _validated_evidence(
    value: object,
    bundle_hash: str,
    elements: object,
) -> dict[str, object]:
    from .validator import ValidationRejected, _resource_check

    try:
        _resource_check(
            value,
            code="evidence_resource_limit",
            max_nodes=2_048,
            max_containers=512,
            max_depth=8,
            max_string_bytes=65_536,
        )
    except ValidationRejected as error:
        raise ValueError("evidence exceeds resource budget") from error
    if not isinstance(value, Mapping):
        raise ValueError("evidence must be a JSON object")
    evidence = dict(value)
    if set(evidence) != {
        "status",
        "bundle_hash",
        "profiles_passed",
        "rounds_passed",
        "validations",
    }:
        raise ValueError("evidence has an invalid Task 4 schema")
    profiles = evidence.get("profiles_passed")
    rounds = evidence.get("rounds_passed")
    if (
        evidence.get("status") != "passed"
        or isinstance(profiles, bool)
        or not isinstance(profiles, int)
        or not 2 <= profiles <= 8
        or isinstance(rounds, bool)
        or rounds != 2
    ):
        raise ValueError("evidence must prove 2..8 profiles and two rounds")
    if evidence["bundle_hash"] != bundle_hash:
        raise ValueError("evidence bundle_hash does not match bundle")
    validations = evidence.get("validations")
    if not isinstance(validations, list) or len(validations) != profiles * 2:
        raise ValueError("evidence validation count is invalid")
    if not isinstance(elements, Mapping) or not elements:
        raise ValueError("evidence bundle elements are invalid")
    candidate_ids = {
        alias: {
            locator["id"]
            for locator in definition["locators"]
            if locator["enabled"] is True
        }
        for alias, definition in elements.items()
    }
    profile_rounds: set[tuple[str, int]] = set()
    profiles_seen: set[str] = set()
    generations: dict[str, set[str]] = {}
    reset_hashes: set[str] = set()
    candidate_baseline: dict[str, str] = {}
    validation_fields = {
        "profile_mask",
        "round_number",
        "reset_evidence_hash",
        "snapshot_hash",
        "page_generation",
        "aliases",
    }
    for validation in validations:
        if not isinstance(validation, Mapping) or set(validation) != validation_fields:
            raise ValueError("evidence validation shape is invalid")
        profile_mask = validation.get("profile_mask")
        round_number = validation.get("round_number")
        if (
            not isinstance(profile_mask, str)
            or not _PROFILE_MASK.fullmatch(profile_mask)
            or isinstance(round_number, bool)
            or round_number not in (1, 2)
        ):
            raise ValueError("evidence profile or round is invalid")
        pair = (profile_mask, round_number)
        if pair in profile_rounds:
            raise ValueError("evidence profile round is duplicated")
        profile_rounds.add(pair)
        profiles_seen.add(profile_mask)
        reset_hash = validation.get("reset_evidence_hash")
        snapshot_hash = validation.get("snapshot_hash")
        page_generation = validation.get("page_generation")
        if not all(
            isinstance(item, str) and _HASH.fullmatch(item)
            for item in (reset_hash, snapshot_hash, page_generation)
        ):
            raise ValueError("evidence freshness hash is invalid")
        if reset_hash in reset_hashes:
            raise ValueError("evidence challenge reset is not fresh")
        reset_hashes.add(reset_hash)
        profile_generations = generations.setdefault(profile_mask, set())
        if page_generation in profile_generations:
            raise ValueError("evidence page generation is not fresh")
        profile_generations.add(page_generation)
        aliases = validation.get("aliases")
        if not isinstance(aliases, Mapping) or set(aliases) != set(candidate_ids):
            raise ValueError("evidence aliases do not match bundle")
        for alias, item in aliases.items():
            if (
                not isinstance(item, Mapping)
                or set(item) != {"status", "candidate_id"}
                or item.get("status") != "ok"
            ):
                raise ValueError("evidence candidate shape is invalid")
            candidate_id = item.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or not _CANDIDATE_ID.fullmatch(candidate_id)
                or candidate_id not in candidate_ids[alias]
            ):
                raise ValueError("evidence candidate is invalid")
            if (
                alias in candidate_baseline
                and candidate_baseline[alias] != candidate_id
            ):
                raise ValueError("evidence candidate changed between validations")
            candidate_baseline.setdefault(alias, candidate_id)
    if len(profiles_seen) != profiles or any(
        (profile, round_number) not in profile_rounds
        for profile in profiles_seen
        for round_number in (1, 2)
    ):
        raise ValueError("evidence profile rounds are incomplete")
    encoded = _canonical_json(evidence)
    if len(encoded.encode("utf-8")) > 65_536:
        raise ValueError("evidence exceeds resource budget")
    return evidence


def _publication_payload_json(
    version_id: str,
    base_version_id: str,
    bundle: Mapping[str, object],
    *,
    lease_owner: str = "",
) -> str:
    payload = {
        "version": version_id,
        "expected_previous_version": base_version_id,
        "bundle": dict(bundle),
    }
    if lease_owner:
        payload["lease_owner"] = lease_owner
    return _canonical_json(payload)


def _alert_record(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    result["aliases"] = json.loads(result.pop("aliases_json"))
    result["strategy_ids"] = json.loads(result.pop("strategy_ids_json"))
    result["details"] = json.loads(result.pop("details_json"))
    result.pop("fingerprint", None)
    return result


def _alert_webhook_payload(
    row: sqlite3.Row,
    *,
    event_type: str,
) -> str:
    alert = _alert_record(row)
    return _json(
        {
            "event_type": event_type,
            "alert_id": alert["id"],
            "site": alert["site"],
            "environment": alert["environment"],
            "status": alert["status"],
            "failure_class": alert["failure_class"],
            "aliases": alert["aliases"],
            "strategy_ids": alert["strategy_ids"],
            "active_version": alert["active_version"],
            "first_seen_at": alert["first_seen_at"],
            "last_seen_at": alert["last_seen_at"],
            "occurrence_count": alert["occurrence_count"],
            "details": alert["details"],
        }
    )


def _same_version_content(
    row: sqlite3.Row,
    *,
    site: str,
    environment: str,
    probe_run_id: int | None,
    lease_owner: str,
    base_version_id: str,
    bundle_json: str,
    bundle_hash: str,
    evidence_json: str,
    model_id: str,
    prompt_version: str,
) -> bool:
    return (
        row["site"] == site
        and row["environment"] == environment
        and row["probe_run_id"] == probe_run_id
        and row["lease_owner"] == lease_owner
        and row["base_version_id"] == base_version_id
        and row["bundle_json"] == bundle_json
        and row["bundle_hash"] == bundle_hash
        and row["evidence_json"] == evidence_json
        and row["model_id"] == model_id
        and row["prompt_version"] == prompt_version
    )


def _ensure_single_outbox(
    connection: sqlite3.Connection,
    *,
    version_id: str,
    payload_json: str,
    timestamp: str,
) -> None:
    rows = connection.execute(
        """
        SELECT id, status
        FROM publication_outbox
        WHERE aggregate_id = ?
        ORDER BY id
        """,
        (version_id,),
    ).fetchall()
    if len(rows) > 1:
        raise RuntimeError("selector version has duplicate outbox events")
    if not rows:
        connection.execute(
            """
            INSERT INTO publication_outbox (
                event_type,
                aggregate_id,
                payload_json,
                status,
                next_attempt_at,
                created_at
            ) VALUES (
                'selector_version_validated',
                ?,
                ?,
                'pending',
                ?,
                ?
            )
            """,
            (version_id, payload_json, timestamp, timestamp),
        )
        return
    status = rows[0]["status"]
    if status in {"pending", "processing", "completed"}:
        return
    if status not in {"conflict", "publication_failed"}:
        raise RuntimeError("selector version outbox status is invalid")
    connection.execute(
        """
        UPDATE publication_outbox
        SET payload_json = ?,
            status = 'pending',
            next_attempt_at = ?,
            claim_token = '',
            lease_until = NULL,
            last_error = '',
            completed_at = NULL
        WHERE id = ?
        """,
        (payload_json, timestamp, rows[0]["id"]),
    )
    connection.execute(
        """
        UPDATE selector_versions
        SET status = 'validated', published_at = NULL
        WHERE id = ?
        """,
        (version_id,),
    )


def _complete_staged_element_request(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    request_id: str,
    version_id: str,
    completed_at: str,
) -> None:
    staged_result = _decode_json_object(
        row["request_staged_result_json"],
        "staged element request result",
    )
    element_id = str(row["request_element_id"])
    safe_result = _safe_element_request_result(staged_result, element_id)
    safe_result.update(
        {
            "status": "published",
            "published": True,
            "reconciled": True,
            "new_version": version_id,
        }
    )
    candidates = _element_result_candidates(safe_result, element_id)
    validation = {
        "status": "published",
        "last_validated_at": completed_at,
        "rounds": safe_result.get("rounds", []),
        "repairs": safe_result.get("repairs", []),
    }
    cursor = connection.execute(
        """
        UPDATE element_request_outbox
        SET status = 'completed',
            claim_token = '',
            lease_until = NULL,
            completed_at = ?,
            error_code = '',
            result_json = ?,
            updated_at = ?
        WHERE request_id = ?
          AND status = 'publishing'
          AND claim_generation = ?
          AND staged_version_id = ?
        """,
        (
            completed_at,
            _json(safe_result),
            completed_at,
            request_id,
            int(row["element_request_generation"]),
            version_id,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("staged element request completion fence failed")
    if candidates:
        connection.execute(
            """
            UPDATE element_drafts
            SET candidates_json = ?,
                validation_json = ?,
                updated_at = ?
            WHERE element_id = ?
            """,
            (
                _json(candidates),
                _json(validation),
                completed_at,
                element_id,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE element_drafts
            SET validation_json = ?,
                updated_at = ?
            WHERE element_id = ?
            """,
            (_json(validation), completed_at, element_id),
        )
    primary_locator_type = (
        str(candidates[0].get("type") or "") if candidates else ""
    )
    connection.execute(
        """
        UPDATE managed_elements
        SET management_source = 'automatic',
            published_status = 'healthy',
            draft_status = NULL,
            active_version_id = ?,
            primary_locator_type = CASE
                WHEN ? != '' THEN ?
                ELSE primary_locator_type
            END,
            last_validated_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            version_id,
            primary_locator_type,
            primary_locator_type,
            completed_at,
            completed_at,
            element_id,
        ),
    )
    _bump_element_catalog_revision(connection)
    _insert_selector_management_audit(
        connection,
        actor_user_id=int(row["request_actor_user_id"]),
        actor_username=str(row["request_actor_username"]),
        event_type="element_validate_completed",
        target_id=element_id,
        details={
            "request_id": request_id,
            "attempt_count": int(row["request_attempt_count"]),
        },
        created_at=completed_at,
    )


def _fail_staged_element_request(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    request_generation: int,
    version_id: str,
    error_code: str,
    completed_at: str,
) -> None:
    row = connection.execute(
        """
        SELECT *
        FROM element_request_outbox
        WHERE request_id = ?
          AND status = 'publishing'
          AND claim_generation = ?
          AND staged_version_id = ?
        """,
        (request_id, request_generation, version_id),
    ).fetchone()
    if row is None:
        return
    connection.execute(
        """
        UPDATE element_request_outbox
        SET status = 'failed',
            claim_token = '',
            lease_until = NULL,
            next_attempt_at = ?,
            completed_at = ?,
            error_code = ?,
            updated_at = ?
        WHERE request_id = ?
          AND status = 'publishing'
          AND claim_generation = ?
          AND staged_version_id = ?
        """,
        (
            completed_at,
            completed_at,
            error_code,
            completed_at,
            request_id,
            request_generation,
            version_id,
        ),
    )
    validation = {
        "status": "failed",
        "failure_code": error_code,
        "request_id": request_id,
        "rounds": [],
        "repairs": [],
    }
    connection.execute(
        """
        UPDATE element_drafts
        SET validation_json = ?,
            updated_at = ?
        WHERE element_id = ?
        """,
        (_json(validation), completed_at, row["element_id"]),
    )
    connection.execute(
        """
        UPDATE managed_elements
        SET draft_status = 'draft',
            updated_at = ?
        WHERE id = ?
        """,
        (completed_at, row["element_id"]),
    )
    _bump_element_catalog_revision(connection)
    _insert_selector_management_audit(
        connection,
        actor_user_id=int(row["actor_user_id"]),
        actor_username=str(row["actor_username"]),
        event_type="element_validate_failed",
        target_id=str(row["element_id"]),
        details={
            "request_id": request_id,
            "attempt_count": int(row["attempt_count"]),
            "error_code": error_code,
        },
        created_at=completed_at,
    )


def _gate_text(value: object, name: str) -> str:
    text = _required_text(value, name)
    if (
        len(text) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise ValueError(f"{name} is invalid")
    return text


def _assert_no_element_request_in_progress(
    connection: sqlite3.Connection,
    element_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT 1
        FROM element_request_outbox
        WHERE element_id = ?
          AND status IN ('pending', 'processing', 'publishing')
        LIMIT 1
        """,
        (element_id,),
    ).fetchone()
    if row is not None:
        raise ElementRequestInProgressError(element_id)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _gate_text_sequence(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array of strings")
    return tuple(sorted({_gate_text(item, name) for item in value}))


def _validated_dependency_rows(
    value: object,
) -> list[tuple[str, str, str, str, str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("dependencies must be an array")
    rows: list[tuple[str, str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) not in (4, 5):
            raise ValueError("dependency row must contain four or five strings")
        alias = _gate_text(item[0], "dependency alias")
        strategy_id = _gate_text(item[1], "dependency strategy_id")
        action_id = _gate_text(item[2], "dependency action_id")
        action_type = _gate_text(item[3], "dependency action_type")
        strategy_name = (
            _gate_text(item[4], "dependency strategy_name")
            if len(item) == 5 and item[4]
            else ""
        )
        identity = (alias, strategy_id, action_id)
        if identity in seen:
            raise ValueError("dependency rows must be unique")
        seen.add(identity)
        rows.append(
            (
                alias,
                strategy_id,
                action_id,
                action_type,
                strategy_name,
            )
        )
    return sorted(rows)


def _validated_gate_reasons(
    value: object,
) -> list[tuple[str, str, str, str, str, list[str], str, str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("gate reasons must be an array")
    prepared: list[
        tuple[str, str, str, str, str, list[str], str, str]
    ] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("gate reason has an invalid shape")
        source = item.get("source")
        common = {
            "strategy_id",
            "source",
            "reason_code",
            "aliases",
            "selector_version_id",
            "created_by",
        }
        expected = (
            common | {"site", "environment"}
            if source == "probe"
            else common
        )
        if set(item) != expected:
            raise ValueError("gate reason has an invalid shape")
        if source not in {"probe", "manual"}:
            raise ValueError("gate source must be probe or manual")
        if source == "probe":
            site = _key_segment(item.get("site"), "site")
            environment = _key_segment(
                item.get("environment"),
                "environment",
            )
        else:
            site = ""
            environment = ""
        selector_version = item["selector_version_id"]
        if (
            not isinstance(selector_version, str)
            or selector_version != selector_version.strip()
            or len(selector_version) > 128
        ):
            raise ValueError("selector_version_id must be a trimmed string")
        aliases = list(_gate_text_sequence(item["aliases"], "gate aliases"))
        prepared.append(
            (
                _gate_text(item["strategy_id"], "strategy_id"),
                source,
                site,
                environment,
                _gate_text(item["reason_code"], "reason_code"),
                aliases,
                selector_version,
                _gate_text(item["created_by"], "created_by"),
            )
        )
    return prepared


def _decode_string_array(value: object, name: str) -> list[str]:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} is corrupt")
    try:
        decoded = json.loads(value)
    except (RecursionError, TypeError, ValueError) as error:
        raise RuntimeError(f"{name} is corrupt") from error
    return list(_gate_text_sequence(decoded, name))


def _bump_gate_revisions(
    connection: sqlite3.Connection,
    strategy_ids: set[str],
) -> None:
    for strategy_id in sorted(strategy_ids):
        connection.execute(
            """
            INSERT INTO strategy_gate_revisions (strategy_id, revision)
            VALUES (?, 1)
            ON CONFLICT(strategy_id) DO UPDATE
            SET revision = strategy_gate_revisions.revision + 1
            """,
            (strategy_id,),
        )


def _bump_element_catalog_revision(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        UPDATE element_catalog_state
        SET revision = revision + 1
        WHERE singleton = 1
        """
    )


def _insert_selector_management_audit(
    connection: sqlite3.Connection,
    *,
    actor_user_id: int,
    actor_username: str,
    event_type: str,
    target_type: str = "element",
    target_id: str,
    result: str = "succeeded",
    details: Mapping[str, object],
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO selector_management_audit_events (
            actor_user_id,
            actor_username,
            event_type,
            target_type,
            target_id,
            result,
            details_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_user_id,
            actor_username,
            _gate_text(event_type, "event_type"),
            _gate_text(target_type, "target_type"),
            _gate_text(target_id, "target_id"),
            _gate_text(result, "result"),
            _json(dict(details)),
            _iso_timestamp(created_at, "created_at"),
        ),
    )


def _decode_json_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} is corrupt")
    try:
        decoded = json.loads(value)
    except (RecursionError, TypeError, ValueError) as error:
        raise RuntimeError(f"{name} is corrupt") from error
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{name} is corrupt")
    return decoded


def _element_request_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "request_id": str(row["request_id"]),
        "request_type": str(row["request_type"]),
        "element_id": str(row["element_id"]),
        "expected_revision": int(row["expected_revision"]),
        "contract": _decode_json_object(
            row["contract_json"],
            "element request contract",
        ),
        "actor_user_id": int(row["actor_user_id"]),
        "actor_username": str(row["actor_username"]),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "claim_token": str(row["claim_token"]),
        "claim_generation": int(row["claim_generation"]),
        "lease_until": row["lease_until"],
        "next_attempt_at": str(row["next_attempt_at"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error_code": str(row["error_code"]),
        "result": _decode_json_object(
            row["result_json"],
            "element request result",
        ),
        "staged_version_id": str(row["staged_version_id"]),
        "staged_result": _decode_json_object(
            row["staged_result_json"],
            "staged element request result",
        ),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _safe_element_request_result(
    value: Mapping[str, object],
    element_id: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for field in ("status", "failure_code", "version", "new_version"):
        selected = value.get(field)
        if (
            isinstance(selected, str)
            and selected
            and selected == selected.strip()
            and len(selected) <= 128
        ):
            result[field] = selected
    for field in ("published", "reconciled"):
        selected = value.get(field)
        if isinstance(selected, bool):
            result[field] = selected
    candidate = value.get("candidate")
    if isinstance(candidate, Mapping):
        raw_elements = candidate.get("elements")
        raw_definition = (
            raw_elements.get(element_id)
            if isinstance(raw_elements, Mapping)
            else candidate
        )
        if isinstance(raw_definition, Mapping):
            try:
                result["candidate"] = normalize_element_definitions(
                    {element_id: raw_definition}
                )[element_id]
            except ValueError:
                pass
    evidence = value.get("validation_evidence")
    raw_rounds = (
        evidence.get("rounds", evidence.get("validations"))
        if isinstance(evidence, Mapping)
        else value.get("rounds")
    )
    result["rounds"] = _safe_element_rounds(raw_rounds)
    raw_repairs = value.get("repairs", value.get("repair_history"))
    result["repairs"] = _safe_element_repairs(raw_repairs)
    return result


def _safe_element_rounds(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    result: list[dict[str, object]] = []
    fields = {
        "profile_mask",
        "round_number",
        "result",
        "status",
        "failure_code",
        "page_state",
        "match_count",
        "role_name_result",
        "visible",
        "in_viewport",
        "actionable",
        "postcondition_result",
        "started_at",
        "finished_at",
    }
    for raw in value[:20]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, object] = {}
        for field in fields:
            selected = raw.get(field)
            if isinstance(selected, bool):
                item[field] = selected
            elif isinstance(selected, int):
                item[field] = selected
            elif isinstance(selected, str) and len(selected) <= 128:
                item[field] = selected
        result.append(item)
    return result


def _safe_element_repairs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    result: list[dict[str, object]] = []
    fields = {
        "attempt",
        "previous_method",
        "failure_code",
        "match_count",
        "new_method",
        "prompt_version",
        "model_id",
        "result",
    }
    for raw in value[:3]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, object] = {}
        for field in fields:
            selected = raw.get(field)
            if isinstance(selected, int) and not isinstance(selected, bool):
                item[field] = selected
            elif isinstance(selected, str) and len(selected) <= 128:
                item[field] = selected
        result.append(item)
    return result


def _element_result_candidates(
    result: Mapping[str, object],
    element_id: str,
) -> list[dict[str, object]]:
    candidate = result.get("candidate")
    if not isinstance(candidate, Mapping):
        return []
    locators = candidate.get("locators")
    if not isinstance(locators, list):
        return []
    try:
        normalized = normalize_element_definitions(
            {
                element_id: {
                    "scope": candidate.get("scope"),
                    "locators": locators,
                }
            }
        )
    except ValueError:
        return []
    return list(normalized[element_id]["locators"])


def _prepare_probe_policy(
    value: Mapping[str, object] | None,
) -> tuple[str, str, str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "site",
        "environment",
        "outcome",
        "occurred_at",
    }:
        raise ValueError("probe policy has an invalid shape")
    outcome = value.get("outcome")
    if outcome not in {"infrastructure", "selector_failure", "validated"}:
        raise ValueError("probe policy outcome is invalid")
    return (
        _key_segment(value.get("site"), "site"),
        _key_segment(value.get("environment"), "environment"),
        str(outcome),
        _iso_timestamp(value.get("occurred_at"), "occurred_at"),
    )


def _prepare_probe_effect(
    value: Mapping[str, object] | None,
) -> tuple[str, str, str, str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "key",
        "type",
        "payload",
    }:
        raise ValueError("probe effect has an invalid shape")
    effect_key = _required_text(value.get("key"), "effect key")
    if len(effect_key) > 256:
        raise ValueError("effect key is too long")
    event_type = value.get("type")
    if event_type not in {
        "selector_failure",
        "probe_unavailable",
        "probe_stale",
        "recovery",
    }:
        raise ValueError("probe effect type is invalid")
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("probe effect payload must be an object")
    payload_value = dict(payload)
    site, environment = _probe_effect_scope(payload_value)
    payload_json = _json(payload_value)
    if len(payload_json.encode("utf-8")) > 65_536:
        raise ValueError("probe effect payload is too large")
    return effect_key, str(event_type), site, environment, payload_json


def _update_probe_health(
    connection: sqlite3.Connection,
    site: str,
    environment: str,
    outcome: str,
    occurred_at: str,
) -> None:
    row = connection.execute(
        """
        SELECT failure_started_at, retry_count, last_validated_at
        FROM probe_health_state
        WHERE site = ? AND environment = ?
        """,
        (site, environment),
    ).fetchone()
    failure_started_at = (
        str(row["failure_started_at"]) if row is not None else ""
    )
    retry_count = int(row["retry_count"]) if row is not None else 0
    last_validated_at = (
        str(row["last_validated_at"]) if row is not None else ""
    )
    if outcome == "infrastructure":
        failure_started_at = failure_started_at or occurred_at
        retry_count += 1
        delay = INFRASTRUCTURE_RETRY_SECONDS[
            min(retry_count - 1, len(INFRASTRUCTURE_RETRY_SECONDS) - 1)
        ]
        next_retry_at = (
            datetime.fromisoformat(occurred_at) + timedelta(seconds=delay)
        ).isoformat()
    elif outcome == "validated":
        failure_started_at = ""
        retry_count = 0
        next_retry_at = ""
        last_validated_at = occurred_at
    else:
        failure_started_at = ""
        retry_count = 0
        next_retry_at = ""
    connection.execute(
        """
        INSERT INTO probe_health_state (
            site,
            environment,
            failure_started_at,
            retry_count,
            next_retry_at,
            last_validated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(site, environment) DO UPDATE SET
            failure_started_at = excluded.failure_started_at,
            retry_count = excluded.retry_count,
            next_retry_at = excluded.next_retry_at,
            last_validated_at = excluded.last_validated_at
        """,
        (
            site,
            environment,
            failure_started_at,
            retry_count,
            next_retry_at,
            last_validated_at,
        ),
    )


def _probe_effect_scope(
    payload: Mapping[str, object],
) -> tuple[str, str]:
    return (
        _key_segment(payload.get("site"), "site"),
        _key_segment(payload.get("environment"), "environment"),
    )


def _upsert_probe_gate_reason(
    connection: sqlite3.Connection,
    *,
    strategy_id: str,
    site: str,
    environment: str,
    reason_code: str,
    aliases: Sequence[str],
    selector_version_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT id, aliases_json
        FROM strategy_gate_reasons
        WHERE strategy_id = ?
          AND source = 'probe'
          AND site = ?
          AND environment = ?
          AND reason_code = ?
          AND selector_version_id = ?
          AND cleared_at IS NULL
        """,
        (
            strategy_id,
            site,
            environment,
            reason_code,
            selector_version_id,
        ),
    ).fetchone()
    selected_aliases = sorted(set(aliases))
    if row is None:
        connection.execute(
            """
            INSERT INTO strategy_gate_reasons (
                strategy_id,
                source,
                site,
                environment,
                reason_code,
                aliases_json,
                selector_version_id,
                created_at,
                created_by
            ) VALUES (
                ?, 'probe', ?, ?, ?, ?, ?, ?, 'selector-probe'
            )
            """,
            (
                strategy_id,
                site,
                environment,
                reason_code,
                _json(selected_aliases),
                selector_version_id,
                _utc_now(),
            ),
        )
        return True
    existing = _decode_string_array(row["aliases_json"], "gate aliases")
    merged = sorted(set(existing) | set(selected_aliases))
    if merged == existing:
        return False
    connection.execute(
        """
        UPDATE strategy_gate_reasons
        SET aliases_json = ?
        WHERE id = ? AND cleared_at IS NULL
        """,
        (_json(merged), row["id"]),
    )
    return True


def _apply_selector_failure_effect(
    connection: sqlite3.Connection,
    payload: Mapping[str, object],
) -> dict[str, object]:
    site, environment = _probe_effect_scope(payload)
    aliases = sorted(
        set(_string_sequence(payload.get("aliases"), "aliases"))
    )
    if not aliases:
        raise ValueError("selector failure effect needs aliases")
    active_version = _required_text_or_empty(
        payload.get("active_version"),
        "active_version",
    )
    failure_code = _required_text(
        payload.get("failure_code"),
        "failure_code",
    )
    match_count = payload.get("match_count")
    if (
        match_count is not None
        and (
            isinstance(match_count, bool)
            or not isinstance(match_count, int)
            or match_count < 0
        )
    ):
        raise ValueError("match_count is invalid")
    required_state = _required_text_or_empty(
        payload.get("required_state", ""),
        "required_state",
    )
    screenshot_path = _required_text_or_empty(
        payload.get("screenshot_path", ""),
        "screenshot_path",
    )
    placeholders = ",".join("?" for _item in aliases)
    dependencies = connection.execute(
        f"""
        SELECT alias, strategy_id
        FROM strategy_dependencies
        WHERE alias IN ({placeholders})
        ORDER BY strategy_id, alias
        """,
        aliases,
    ).fetchall()
    by_strategy: dict[str, set[str]] = {}
    for row in dependencies:
        by_strategy.setdefault(str(row["strategy_id"]), set()).add(
            str(row["alias"])
        )
    changed: set[str] = set()
    for strategy_id, strategy_aliases in by_strategy.items():
        if _upsert_probe_gate_reason(
            connection,
            strategy_id=strategy_id,
            site=site,
            environment=environment,
            reason_code="selector_validation_failed",
            aliases=sorted(strategy_aliases),
            selector_version_id=active_version,
        ):
            changed.add(strategy_id)
    _bump_gate_revisions(connection, changed)
    strategy_ids = sorted(by_strategy)
    fingerprint = hashlib.sha256(
        (
            f"{site}\0{environment}\0selector_validation_failed\0"
            f"{','.join(aliases)}\0{active_version}"
        ).encode()
    ).hexdigest()
    row = connection.execute(
        """
        SELECT id
        FROM probe_alerts
        WHERE fingerprint = ?
          AND status IN ('open', 'acknowledged')
        """,
        (fingerprint,),
    ).fetchone()
    timestamp = _iso_timestamp(
        payload.get("occurred_at", _utc_now()),
        "occurred_at",
    )
    details_json = _json(
        {
            "failure_code": failure_code,
            "match_count": match_count,
            "required_state": required_state,
        }
    )
    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO probe_alerts (
                fingerprint,
                site,
                environment,
                status,
                failure_class,
                aliases_json,
                strategy_ids_json,
                active_version,
                first_seen_at,
                last_seen_at,
                occurrence_count,
                details_json
            ) VALUES (
                ?, ?, ?, 'open', 'selector_validation_failed',
                ?, ?, ?, ?, ?, 1, ?
            )
            """,
            (
                fingerprint,
                site,
                environment,
                _json(aliases),
                _json(strategy_ids),
                active_version,
                timestamp,
                timestamp,
                details_json,
            ),
        )
        alert_id = int(cursor.lastrowid)
        alert = connection.execute(
            "SELECT * FROM probe_alerts WHERE id = ?",
            (alert_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO webhook_outbox (
                alert_id,
                event_type,
                payload_json,
                status,
                next_attempt_at,
                created_at
            ) VALUES (?, 'alert_opened', ?, 'pending', ?, ?)
            """,
            (
                alert_id,
                _alert_webhook_payload(alert, event_type="alert_opened"),
                timestamp,
                timestamp,
            ),
        )
    else:
        alert_id = int(row["id"])
        connection.execute(
            """
            UPDATE probe_alerts
            SET last_seen_at = ?,
                occurrence_count = occurrence_count + 1,
                details_json = ?,
                aliases_json = ?,
                strategy_ids_json = ?
            WHERE id = ?
            """,
            (
                timestamp,
                details_json,
                _json(aliases),
                _json(strategy_ids),
                alert_id,
            ),
        )
    return {
        "strategy_ids": strategy_ids,
        "alert_id": alert_id,
        "screenshot_path": screenshot_path,
    }


def _upsert_probe_unavailable_alert(
    connection: sqlite3.Connection,
    payload: Mapping[str, object],
    *,
    strategy_ids: Sequence[str],
) -> dict[str, object]:
    site, environment = _probe_effect_scope(payload)
    active_version = _required_text_or_empty(
        payload.get("active_version", ""),
        "active_version",
    )
    failure_started_at = _iso_timestamp(
        payload.get("failure_started_at"),
        "failure_started_at",
    )
    selected_strategy_ids = sorted(set(strategy_ids))
    failure_code = _required_text(
        payload.get("failure_code", "probe_unavailable"),
        "failure_code",
    )
    timestamp = _iso_timestamp(
        payload.get("occurred_at", _utc_now()),
        "occurred_at",
    )
    fingerprint = hashlib.sha256(
        (
            f"{site}\0{environment}\0probe_unavailable\0\0"
            f"{active_version}"
        ).encode()
    ).hexdigest()
    alert_row = connection.execute(
        """
        SELECT id
        FROM probe_alerts
        WHERE fingerprint = ?
          AND status IN ('open', 'acknowledged')
        """,
        (fingerprint,),
    ).fetchone()
    details_json = _json(
        {
            "failure_code": failure_code,
            "failure_started_at": failure_started_at,
        }
    )
    if alert_row is None:
        cursor = connection.execute(
            """
            INSERT INTO probe_alerts (
                fingerprint,
                site,
                environment,
                status,
                failure_class,
                aliases_json,
                strategy_ids_json,
                active_version,
                first_seen_at,
                last_seen_at,
                occurrence_count,
                details_json
            ) VALUES (
                ?, ?, ?, 'open', 'probe_unavailable',
                '[]', ?, ?, ?, ?, 1, ?
            )
            """,
            (
                fingerprint,
                site,
                environment,
                _json(selected_strategy_ids),
                active_version,
                timestamp,
                timestamp,
                details_json,
            ),
        )
        alert_id = int(cursor.lastrowid)
        alert = connection.execute(
            "SELECT * FROM probe_alerts WHERE id = ?",
            (alert_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO webhook_outbox (
                alert_id,
                event_type,
                payload_json,
                status,
                next_attempt_at,
                created_at
            ) VALUES (?, 'alert_opened', ?, 'pending', ?, ?)
            """,
            (
                alert_id,
                _alert_webhook_payload(alert, event_type="alert_opened"),
                timestamp,
                timestamp,
            ),
        )
    else:
        alert_id = int(alert_row["id"])
        connection.execute(
            """
            UPDATE probe_alerts
            SET last_seen_at = ?,
                occurrence_count = occurrence_count + 1,
                strategy_ids_json = ?,
                details_json = ?
            WHERE id = ?
            """,
            (
                timestamp,
                _json(selected_strategy_ids),
                details_json,
                alert_id,
            ),
        )
    return {
        "strategy_ids": selected_strategy_ids,
        "alert_id": alert_id,
    }


def _apply_probe_unavailable_effect(
    connection: sqlite3.Connection,
    payload: Mapping[str, object],
) -> dict[str, object]:
    strategy_ids = [
        str(row["strategy_id"])
        for row in connection.execute(
            """
            SELECT DISTINCT strategy_id
            FROM strategy_dependencies
            ORDER BY strategy_id
            """
        ).fetchall()
    ]
    return _upsert_probe_unavailable_alert(
        connection,
        payload,
        strategy_ids=strategy_ids,
    )


def _apply_probe_stale_effect(
    connection: sqlite3.Connection,
    payload: Mapping[str, object],
) -> dict[str, object]:
    site, environment = _probe_effect_scope(payload)
    active_version = _required_text_or_empty(
        payload.get("active_version", ""),
        "active_version",
    )
    _iso_timestamp(
        payload.get("failure_started_at"),
        "failure_started_at",
    )
    rows = connection.execute(
        """
        SELECT strategy_id, alias
        FROM strategy_dependencies
        ORDER BY strategy_id, alias
        """
    ).fetchall()
    by_strategy: dict[str, set[str]] = {}
    for row in rows:
        by_strategy.setdefault(str(row["strategy_id"]), set()).add(
            str(row["alias"])
        )
    changed: set[str] = set()
    for strategy_id, aliases in by_strategy.items():
        if _upsert_probe_gate_reason(
            connection,
            strategy_id=strategy_id,
            site=site,
            environment=environment,
            reason_code="probe_validation_stale",
            aliases=sorted(aliases),
            selector_version_id=active_version,
        ):
            changed.add(strategy_id)
    _bump_gate_revisions(connection, changed)
    strategy_ids = sorted(by_strategy)
    return _upsert_probe_unavailable_alert(
        connection,
        payload,
        strategy_ids=strategy_ids,
    )


def _apply_recovery_effect(
    connection: sqlite3.Connection,
    payload: Mapping[str, object],
) -> dict[str, object]:
    site, environment = _probe_effect_scope(payload)
    selector_version_id = _required_text(
        payload.get("selector_version_id"),
        "selector_version_id",
    )
    bundle_hash = payload.get("bundle_hash")
    if not isinstance(bundle_hash, str) or not _HASH.fullmatch(bundle_hash):
        raise ValueError("bundle_hash is invalid")
    covered = set(
        _string_sequence(payload.get("covered_aliases"), "covered_aliases")
    )
    if not covered:
        raise ValueError("recovery effect needs covered aliases")
    rows = connection.execute(
        """
        SELECT id, strategy_id, aliases_json
        FROM strategy_gate_reasons
        WHERE source = 'probe'
          AND site = ?
          AND environment = ?
          AND reason_code IN (
              'selector_validation_failed',
              'probe_validation_stale',
              'registry_unavailable'
          )
          AND cleared_at IS NULL
        ORDER BY id
        """,
        (site, environment),
    ).fetchall()
    clear_ids: list[int] = []
    strategies: set[str] = set()
    for row in rows:
        aliases = set(
            _decode_string_array(row["aliases_json"], "gate aliases")
        )
        if not aliases:
            dependency_rows = connection.execute(
                """
                SELECT DISTINCT alias
                FROM strategy_dependencies
                WHERE strategy_id = ?
                """,
                (row["strategy_id"],),
            ).fetchall()
            aliases = {str(item["alias"]) for item in dependency_rows}
        if aliases and aliases <= covered:
            clear_ids.append(int(row["id"]))
            strategies.add(str(row["strategy_id"]))
    timestamp = _iso_timestamp(
        payload.get("occurred_at", _utc_now()),
        "occurred_at",
    )
    if clear_ids:
        placeholders = ",".join("?" for _item in clear_ids)
        connection.execute(
            f"""
            UPDATE strategy_gate_reasons
            SET cleared_at = ?, cleared_by = ?
            WHERE id IN ({placeholders}) AND cleared_at IS NULL
            """,
            (
                timestamp,
                f"probe:{selector_version_id}",
                *clear_ids,
            ),
        )
        _bump_gate_revisions(connection, strategies)
    alert_rows = connection.execute(
        """
        SELECT *
        FROM probe_alerts
        WHERE site = ?
          AND environment = ?
          AND status IN ('open', 'acknowledged')
          AND failure_class IN (
              'selector_validation_failed',
              'probe_unavailable'
          )
        ORDER BY id
        """,
        (site, environment),
    ).fetchall()
    resolved: list[sqlite3.Row] = []
    for row in alert_rows:
        aliases = set(
            _decode_string_array(row["aliases_json"], "alert aliases")
        )
        should_resolve = (
            row["failure_class"] == "probe_unavailable"
            or (
                row["failure_class"] == "selector_validation_failed"
                and bool(aliases)
                and aliases <= covered
            )
        )
        if should_resolve:
            connection.execute(
                """
                UPDATE probe_alerts
                SET status = 'resolved', resolved_at = ?
                WHERE id = ? AND status IN ('open', 'acknowledged')
                """,
                (timestamp, row["id"]),
            )
            resolved_row = connection.execute(
                "SELECT * FROM probe_alerts WHERE id = ?",
                (row["id"],),
            ).fetchone()
            resolved.append(resolved_row)
    if resolved:
        recovery_alert = resolved[0]
        connection.execute(
            """
            INSERT INTO webhook_outbox (
                alert_id,
                event_type,
                payload_json,
                status,
                next_attempt_at,
                created_at
            ) VALUES (?, 'alert_recovered', ?, 'pending', ?, ?)
            """,
            (
                recovery_alert["id"],
                _alert_webhook_payload(
                    recovery_alert,
                    event_type="alert_recovered",
                ),
                timestamp,
                timestamp,
            ),
        )
    return {
        "strategy_ids": sorted(strategies),
        "resolved_alert_ids": [int(row["id"]) for row in resolved],
    }


def _version_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "site": row["site"],
        "environment": row["environment"],
        "status": row["status"],
        "base_version_id": row["base_version_id"],
        "bundle": _decode_json_object(row["bundle_json"], "version bundle"),
        "bundle_hash": row["bundle_hash"],
        "evidence": _decode_json_object(row["evidence_json"], "version evidence"),
        "model_id": row["model_id"],
        "prompt_version": row["prompt_version"],
        "created_at": row["created_at"],
        "validated_at": row["validated_at"],
        "published_at": row["published_at"],
    }
