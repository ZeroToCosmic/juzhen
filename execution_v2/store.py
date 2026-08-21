"""Transactional SQLite persistence for isolated browser execution V2."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from execution_v2.elements import (
    ElementInUseError,
    ElementNotFoundError,
    ElementRevisionConflictError,
    ElementValidationError,
    normalize_element_definition,
)
from execution_v2.models import JobStatus, ProfileStatus, Stage, utc_now_iso
from execution_v2.strategy import (
    StrategyNotFoundError,
    StrategyRevisionConflictError,
    StrategyValidationError,
    normalize_strategy_definition,
    referenced_element_ids,
)
from remote_actions.checksums import content_checksum, release_checksum
from remote_actions.contracts import validate_release_content
from remote_actions.identifiers import new_action_id, validate_action_id
from remote_actions.publication import (
    ActionIdentityError,
    PublicationActor,
    PublishGateError,
    require_publication_actor,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS elements (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
  kind TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
  definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS element_revisions (
  element_id TEXT NOT NULL, revision INTEGER NOT NULL, definition_json TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(element_id, revision),
  FOREIGN KEY(element_id) REFERENCES elements(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS strategies (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
  revision INTEGER NOT NULL, definition_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_actions (
  strategy_id TEXT NOT NULL, position INTEGER NOT NULL, action_json TEXT NOT NULL,
  PRIMARY KEY(strategy_id, position),
  FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS execution_jobs (
  id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, status TEXT NOT NULL,
  batch_size INTEGER NOT NULL CHECK(batch_size BETWEEN 1 AND 8),
  strategy_snapshot_json TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS execution_profiles (
  job_id TEXT NOT NULL, profile_id TEXT NOT NULL, position INTEGER NOT NULL,
  status TEXT NOT NULL, stage TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
  error_summary TEXT NOT NULL DEFAULT '', close_confirmed INTEGER NOT NULL DEFAULT 0,
  started_at TEXT, finished_at TEXT, PRIMARY KEY(job_id, profile_id),
  FOREIGN KEY(job_id) REFERENCES execution_jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS action_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  action_index INTEGER NOT NULL, action_type TEXT NOT NULL, status TEXT NOT NULL,
  stage TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(job_id, profile_id) REFERENCES execution_profiles(job_id, profile_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS wheel_calibrations (
  scope TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
  direction TEXT NOT NULL, events_json TEXT NOT NULL, sample_count INTEGER NOT NULL,
  replay_validated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, PRIMARY KEY(scope, revision)
);
CREATE TABLE IF NOT EXISTS wheel_calibration_current (
  scope TEXT PRIMARY KEY, revision INTEGER NOT NULL,
  FOREIGN KEY(scope, revision) REFERENCES wheel_calibrations(scope, revision)
);
CREATE TABLE IF NOT EXISTS strategy_action_identities (
  action_id TEXT NOT NULL PRIMARY KEY CHECK(
    length(action_id) = 30 AND substr(action_id, 1, 4) = 'act_'
    AND substr(action_id, 5, 1) GLOB '[0-7]'
    AND substr(action_id, 6) NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'
  ), strategy_id TEXT UNIQUE,
  source_revision INTEGER NOT NULL DEFAULT 1, content_checksum TEXT NOT NULL DEFAULT '',
  tombstoned_at TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE SET NULL
);
CREATE TRIGGER IF NOT EXISTS strategy_action_identity_id_insert_v2
BEFORE INSERT ON strategy_action_identities
WHEN NEW.action_id IS NULL OR NOT (
  length(NEW.action_id) = 30 AND substr(NEW.action_id, 1, 4) = 'act_'
  AND substr(NEW.action_id, 5, 1) GLOB '[0-7]'
  AND substr(NEW.action_id, 6) NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'
)
BEGIN SELECT RAISE(ABORT, 'invalid action_id'); END;
CREATE TABLE IF NOT EXISTS strategy_debug_runs (
  run_id TEXT PRIMARY KEY, action_id TEXT NOT NULL, action_revision INTEGER NOT NULL,
  content_checksum TEXT NOT NULL, status TEXT NOT NULL, finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_debug_gate
  ON strategy_debug_runs(action_id, action_revision, content_checksum, status, finished_at);
CREATE TABLE IF NOT EXISTS strategy_releases (
  action_id TEXT NOT NULL, revision INTEGER NOT NULL, source_revision INTEGER NOT NULL,
  content_checksum TEXT NOT NULL CHECK(
    length(content_checksum) = 71
    AND substr(content_checksum, 1, 7) = 'sha256:'
    AND substr(content_checksum, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  release_checksum TEXT NOT NULL UNIQUE CHECK(
    length(release_checksum) = 71
    AND substr(release_checksum, 1, 7) = 'sha256:'
    AND substr(release_checksum, 8) NOT GLOB '*[^0-9a-f]*'
  ), validation_status TEXT NOT NULL,
  release_json TEXT NOT NULL, debug_run_id TEXT,
  actor TEXT NOT NULL, waiver_reason TEXT NOT NULL DEFAULT '',
  central_revision INTEGER, synced_at TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(action_id, revision),
  UNIQUE(action_id, source_revision, content_checksum)
);
CREATE TRIGGER IF NOT EXISTS strategy_releases_checksum_insert
BEFORE INSERT ON strategy_releases
WHEN NOT (
  length(NEW.content_checksum) = 71
  AND substr(NEW.content_checksum, 1, 7) = 'sha256:'
  AND substr(NEW.content_checksum, 8) NOT GLOB '*[^0-9a-f]*'
  AND length(NEW.release_checksum) = 71
  AND substr(NEW.release_checksum, 1, 7) = 'sha256:'
  AND substr(NEW.release_checksum, 8) NOT GLOB '*[^0-9a-f]*'
)
BEGIN SELECT RAISE(ABORT, 'invalid strategy release checksum'); END;
CREATE TRIGGER IF NOT EXISTS strategy_releases_immutable_update
BEFORE UPDATE ON strategy_releases
WHEN NEW.action_id != OLD.action_id OR NEW.revision != OLD.revision
  OR NEW.source_revision != OLD.source_revision
  OR NEW.content_checksum != OLD.content_checksum
  OR NEW.release_checksum != OLD.release_checksum
  OR NEW.validation_status != OLD.validation_status
  OR NEW.release_json != OLD.release_json
  OR COALESCE(NEW.debug_run_id, '') != COALESCE(OLD.debug_run_id, '')
  OR NEW.actor != OLD.actor OR NEW.waiver_reason != OLD.waiver_reason
  OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'strategy release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_releases_immutable_delete
BEFORE DELETE ON strategy_releases
BEGIN SELECT RAISE(ABORT, 'strategy release is immutable'); END;
"""


_TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.CANCELLED.value,
    JobStatus.CLEANUP_BLOCKED.value,
}
_TERMINAL_PROFILE_STATUSES = {
    ProfileStatus.SUCCEEDED.value,
    ProfileStatus.FAILED.value,
    ProfileStatus.CLEANUP_FAILED.value,
}
_ELEMENT_PURPOSES = {"action", "readiness"}
_ELEMENT_KINDS = {"click", "input", "generic"}
_ELEMENT_STATUSES = {"active", "repick_required", "disabled"}


def _value(value: str | JobStatus | ProfileStatus | Stage) -> str:
    return str(value.value if hasattr(value, "value") else value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ExecutionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(wheel_calibrations)")
            }
            if "replay_validated" not in columns:
                connection.execute(
                    "ALTER TABLE wheel_calibrations "
                    "ADD COLUMN replay_validated INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            strategy_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT s.id FROM strategies s "
                    "LEFT JOIN strategy_action_identities i ON i.strategy_id = s.id "
                    "WHERE i.action_id IS NULL ORDER BY s.id"
                )
            ]
            now = utc_now_iso()
            connection.executemany(
                "INSERT INTO strategy_action_identities"
                "(action_id, strategy_id, source_revision, content_checksum, "
                "tombstoned_at, created_at) VALUES (?, ?, 1, '', NULL, ?)",
                [(new_action_id(), strategy_id, now) for strategy_id in strategy_ids],
            )

    def publish_wheel_calibration(
        self,
        scope: str,
        direction: str,
        events: list[dict[str, Any]],
        sample_count: int,
        *,
        replay_validated: bool,
    ) -> dict[str, Any]:
        if scope != "tiktok_feed" or direction != "down":
            raise ValueError("wheel_calibration_scope_invalid")
        if not isinstance(events, list) or not events or sample_count != 3:
            raise ValueError("wheel_calibration_data_invalid")
        if replay_validated is not True:
            raise ValueError("wheel_calibration_replay_not_validated")
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                "FROM wheel_calibrations WHERE scope = ?",
                (scope,),
            ).fetchone()
            revision = int(row["revision"])
            connection.execute(
                "INSERT INTO wheel_calibrations"
                "(scope, revision, status, direction, events_json, sample_count, "
                "replay_validated, created_at) "
                "VALUES (?, ?, 'validated', ?, ?, ?, 1, ?)",
                (scope, revision, direction, _json(events), sample_count, now),
            )
            connection.execute(
                "INSERT INTO wheel_calibration_current(scope, revision) VALUES (?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET revision = excluded.revision",
                (scope, revision),
            )
        result = self.get_wheel_calibration(scope)
        if result is None:
            raise RuntimeError("wheel_calibration_publish_failed")
        return result

    def get_wheel_calibration(
        self, scope: str = "tiktok_feed"
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT c.scope, c.revision, c.status, c.direction, c.events_json, "
                "c.sample_count, c.replay_validated, c.created_at FROM wheel_calibrations c "
                "JOIN wheel_calibration_current p "
                "ON p.scope = c.scope AND p.revision = c.revision "
                "WHERE c.scope = ? AND c.replay_validated = 1",
                (scope,),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["replay_validated"] = bool(record["replay_validated"])
        record["events"] = json.loads(record.pop("events_json"))
        return record

    def create_element(
        self,
        element_id: str,
        name: str,
        purpose: str,
        kind: str,
        definition: Any,
        *,
        status: str = "active",
    ) -> dict[str, Any]:
        self._validate_element_id(element_id)
        self._validate_element_fields(name, purpose, kind, status)
        normalized_definition = normalize_element_definition(definition)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO elements
                    (id, name, purpose, kind, status, revision, definition_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(element_id),
                    name,
                    purpose,
                    kind,
                    status,
                    1,
                    _json(normalized_definition),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO element_revisions (element_id, revision, definition_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(element_id), 1, _json(normalized_definition), now),
            )
        return self.get_element_or_raise(element_id)

    def get_element(self, element_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM elements WHERE id = ?", (str(element_id),)
            ).fetchone()
        return self._element_record(row)

    def get_element_or_raise(self, element_id: str) -> dict[str, Any]:
        element = self.get_element(element_id)
        if element is None:
            raise ElementNotFoundError(str(element_id))
        return element

    def list_elements(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM elements ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._element_record(row) for row in rows]

    def rename_element(
        self, element_id: str, name: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self._validate_element_name(name)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE elements SET name = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (name, utc_now_iso(), str(element_id), self._expected_revision(expected_revision)),
            )
            self._raise_on_missing_or_conflict(connection, element_id, cursor.rowcount)
        return self.get_element_or_raise(element_id)

    def repick_element(
        self, element_id: str, definition: Any, *, expected_revision: int
    ) -> dict[str, Any]:
        normalized_definition = normalize_element_definition(definition)
        expected = self._expected_revision(expected_revision)
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT revision FROM elements WHERE id = ?", (str(element_id),)
            ).fetchone()
            if row is None:
                raise ElementNotFoundError(str(element_id))
            if row["revision"] != expected:
                raise ElementRevisionConflictError(
                    f"element {element_id} revision conflict: expected {expected}"
                )
            revision = expected + 1
            connection.execute(
                """
                UPDATE elements
                SET definition_json = ?, revision = ?, status = 'active', updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (_json(normalized_definition), revision, now, str(element_id), expected),
            )
            connection.execute(
                """
                INSERT INTO element_revisions (element_id, revision, definition_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(element_id), revision, _json(normalized_definition), now),
            )
        return self.get_element_or_raise(element_id)

    def set_element_status(
        self, element_id: str, status: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self._validate_element_status(status)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE elements SET status = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (status, utc_now_iso(), str(element_id), self._expected_revision(expected_revision)),
            )
            self._raise_on_missing_or_conflict(connection, element_id, cursor.rowcount)
        return self.get_element_or_raise(element_id)

    def count_element_references(self, element_id: str) -> int:
        with self.connect() as connection:
            action_rows = connection.execute("SELECT action_json FROM strategy_actions").fetchall()
            strategy_rows = connection.execute("SELECT definition_json FROM strategies").fetchall()
        action_count = sum(
            1
            for row in action_rows
            if self._action_references_element(row["action_json"], str(element_id))
        )
        readiness_count = sum(
            1
            for row in strategy_rows
            if self._strategy_references_ready_element(row["definition_json"], str(element_id))
        )
        return action_count + readiness_count

    def delete_element(self, element_id: str, *, expected_revision: int) -> None:
        expected = self._expected_revision(expected_revision)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT revision FROM elements WHERE id = ?", (str(element_id),)
            ).fetchone()
            if row is None:
                raise ElementNotFoundError(str(element_id))
            if row["revision"] != expected:
                raise ElementRevisionConflictError(
                    f"element {element_id} revision conflict: expected {expected}"
                )
            referenced = any(
                self._action_references_element(action["action_json"], str(element_id))
                for action in connection.execute("SELECT action_json FROM strategy_actions")
            ) or any(
                self._strategy_references_ready_element(strategy["definition_json"], str(element_id))
                for strategy in connection.execute("SELECT definition_json FROM strategies")
            )
            if referenced:
                raise ElementInUseError(f"element {element_id} is referenced by a strategy action")
            connection.execute("DELETE FROM elements WHERE id = ?", (str(element_id),))

    @staticmethod
    def _element_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["definition"] = json.loads(record.pop("definition_json"))
        return record

    @staticmethod
    def _action_references_element(action_json: str, element_id: str) -> bool:
        try:
            payload = json.loads(action_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

        def contains_reference(value: Any) -> bool:
            if isinstance(value, dict):
                if value.get("element_id") == element_id:
                    return True
                return any(contains_reference(child) for child in value.values())
            if isinstance(value, list):
                return any(contains_reference(child) for child in value)
            return False

        return contains_reference(payload)

    @staticmethod
    def _strategy_references_ready_element(definition_json: str, element_id: str) -> bool:
        try:
            return json.loads(definition_json).get("ready_element_id") == element_id
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _expected_revision(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ElementValidationError("expected_revision must be a positive integer")
        return value

    @staticmethod
    def _validate_element_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ElementValidationError("name must be a non-empty string")

    @classmethod
    def _validate_element_fields(
        cls, name: str, purpose: str, kind: str, status: str
    ) -> None:
        cls._validate_element_name(name)
        if not isinstance(purpose, str) or purpose not in _ELEMENT_PURPOSES:
            raise ElementValidationError("purpose must be action or readiness")
        if not isinstance(kind, str) or kind not in _ELEMENT_KINDS:
            raise ElementValidationError("kind must be click, input, or generic")
        cls._validate_element_status(status)

    @staticmethod
    def _validate_element_status(status: str) -> None:
        if not isinstance(status, str) or status not in _ELEMENT_STATUSES:
            raise ElementValidationError(
                "status must be active, repick_required, or disabled"
            )

    @staticmethod
    def _validate_element_id(element_id: str) -> None:
        if not isinstance(element_id, str) or not element_id.strip():
            raise ElementValidationError("element_id must be a non-empty string")

    @staticmethod
    def _raise_on_missing_or_conflict(
        connection: sqlite3.Connection, element_id: str, rowcount: int
    ) -> None:
        if rowcount:
            return
        exists = connection.execute(
            "SELECT 1 FROM elements WHERE id = ?", (str(element_id),)
        ).fetchone()
        if exists is None:
            raise ElementNotFoundError(str(element_id))
        raise ElementRevisionConflictError(f"element {element_id} revision conflict")

    def create_strategy(
        self,
        strategy_id: str,
        name: str,
        definition: Any,
        enabled: bool = True,
    ) -> dict[str, Any]:
        self._validate_strategy_id(strategy_id)
        self._validate_strategy_name(name)
        self._validate_enabled(enabled)
        now = utc_now_iso()
        with self.connect() as connection:
            normalized = normalize_strategy_definition(
                definition, elements_by_id=self._elements_by_id(connection)
            )
            stored_definition, actions = self._split_strategy_definition(normalized)
            connection.execute(
                """
                INSERT INTO strategies
                    (id, name, enabled, revision, definition_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(strategy_id), name, int(enabled), 1, _json(stored_definition), now, now),
            )
            self._insert_strategy_actions(connection, str(strategy_id), actions)
            connection.execute(
                "INSERT INTO strategy_action_identities"
                "(action_id, strategy_id, source_revision, content_checksum, "
                "tombstoned_at, created_at) VALUES (?, ?, 1, '', NULL, ?)",
                (new_action_id(), str(strategy_id), now),
            )
        return self.get_strategy_or_raise(strategy_id)

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategies WHERE id = ?", (str(strategy_id),)
            ).fetchone()
            return self._strategy_record(connection, row)

    def get_strategy_or_raise(self, strategy_id: str) -> dict[str, Any]:
        strategy = self.get_strategy(strategy_id)
        if strategy is None:
            raise StrategyNotFoundError(str(strategy_id))
        return strategy

    def list_strategies(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategies ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [self._strategy_record(connection, row) for row in rows]

    def list_strategy_actions(self, strategy_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return self._strategy_actions(connection, str(strategy_id))

    def update_strategy(
        self,
        strategy_id: str,
        name: str,
        definition: Any,
        enabled: bool,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._validate_strategy_name(name)
        self._validate_enabled(enabled)
        expected = self._strategy_expected_revision(expected_revision)
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT revision FROM strategies WHERE id = ?", (str(strategy_id),)
            ).fetchone()
            if row is None:
                raise StrategyNotFoundError(str(strategy_id))
            if row["revision"] != expected:
                raise StrategyRevisionConflictError(
                    f"strategy {strategy_id} revision conflict: expected {expected}"
                )
            normalized = normalize_strategy_definition(
                definition, elements_by_id=self._elements_by_id(connection)
            )
            stored_definition, actions = self._split_strategy_definition(normalized)
            revision = expected + 1
            connection.execute(
                """
                UPDATE strategies
                SET name = ?, enabled = ?, revision = ?, definition_json = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (name, int(enabled), revision, _json(stored_definition), now, str(strategy_id), expected),
            )
            connection.execute("DELETE FROM strategy_actions WHERE strategy_id = ?", (str(strategy_id),))
            self._insert_strategy_actions(connection, str(strategy_id), actions)
        return self.get_strategy_or_raise(strategy_id)

    def set_strategy_enabled(
        self, strategy_id: str, enabled: bool, *, expected_revision: int
    ) -> dict[str, Any]:
        self._validate_enabled(enabled)
        expected = self._strategy_expected_revision(expected_revision)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE strategies
                SET enabled = ?, revision = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (int(enabled), expected + 1, utc_now_iso(), str(strategy_id), expected),
            )
            self._raise_strategy_on_missing_or_conflict(connection, strategy_id, cursor.rowcount)
        return self.get_strategy_or_raise(strategy_id)

    def delete_strategy(self, strategy_id: str, *, expected_revision: int) -> None:
        expected = self._strategy_expected_revision(expected_revision)
        with self.connect() as connection:
            identity = connection.execute(
                "SELECT action_id FROM strategy_action_identities WHERE strategy_id = ?",
                (str(strategy_id),),
            ).fetchone()
            if identity is not None:
                connection.execute(
                    "UPDATE strategy_action_identities SET tombstoned_at = ? "
                    "WHERE action_id = ? AND tombstoned_at IS NULL",
                    (utc_now_iso(), identity["action_id"]),
                )
            cursor = connection.execute(
                "DELETE FROM strategies WHERE id = ? AND revision = ?",
                (str(strategy_id), expected),
            )
            self._raise_strategy_on_missing_or_conflict(connection, strategy_id, cursor.rowcount)

    def bind_action_identity(
        self,
        strategy_id: str,
        *,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        selected_id = new_action_id() if action_id is None else action_id
        try:
            validate_action_id(selected_id)
        except ValueError as exc:
            raise ActionIdentityError("invalid action_id") from exc
        now = utc_now_iso()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM strategy_action_identities WHERE action_id = ?",
                (selected_id,),
            ).fetchone()
            if existing is not None:
                raise ActionIdentityError("action_id has already been used")
            strategy = connection.execute(
                "SELECT 1 FROM strategies WHERE id = ?", (str(strategy_id),)
            ).fetchone()
            if strategy is None:
                raise ActionIdentityError("strategy does not exist")
            try:
                connection.execute(
                    "INSERT INTO strategy_action_identities"
                    "(action_id, strategy_id, source_revision, content_checksum, "
                    "tombstoned_at, created_at) VALUES (?, ?, 1, '', NULL, ?)",
                    (selected_id, str(strategy_id), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ActionIdentityError("strategy already has an action identity") from exc
        return self.get_action_publication_metadata(strategy_id)

    def tombstone_action_identity(self, action_id: str, tombstoned_at: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE strategy_action_identities SET tombstoned_at = ? "
                "WHERE action_id = ? AND tombstoned_at IS NULL",
                (str(tombstoned_at), str(action_id)),
            )
            if cursor.rowcount != 1:
                raise ActionIdentityError("active action identity not found")

    def get_action_publication_metadata(self, strategy_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            identity = connection.execute(
                "SELECT * FROM strategy_action_identities WHERE strategy_id = ?",
                (str(strategy_id),),
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM strategies WHERE id = ?", (str(strategy_id),)
            ).fetchone()
            if identity is None or row is None:
                raise ActionIdentityError("action identity not found")
            metadata, _document = self._refresh_strategy_metadata(connection, identity, row)
            return metadata

    def record_debug_run(
        self,
        action_id: str,
        action_revision: int,
        expected_content_checksum: str,
        status: str,
        run_id: str,
        finished_at: str,
    ) -> dict[str, Any]:
        if status not in {
            "RUNNING",
            "SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "UNVERIFIED",
        }:
            raise PublishGateError("invalid debug status")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            metadata, _document = self._metadata_by_action_id(connection, action_id)
            if metadata["tombstoned_at"] is not None:
                raise ActionIdentityError("action identity is tombstoned")
            if (
                metadata["action_revision"] != action_revision
                or metadata["content_checksum"] != expected_content_checksum
            ):
                raise PublishGateError("debug run does not match current action content")
            connection.execute(
                "INSERT INTO strategy_debug_runs"
                "(run_id, action_id, action_revision, content_checksum, status, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, action_id, action_revision, expected_content_checksum, status, finished_at),
            )
        return {
            "run_id": run_id,
            "action_id": action_id,
            "action_revision": action_revision,
            "content_checksum": expected_content_checksum,
            "status": status,
            "finished_at": finished_at,
        }

    def begin_debug_run(self, strategy_id: str, run_id: str) -> dict[str, Any]:
        metadata = self.get_action_publication_metadata(strategy_id)
        return self.record_debug_run(
            metadata["action_id"],
            metadata["action_revision"],
            metadata["content_checksum"],
            "RUNNING",
            run_id,
            "",
        )

    def complete_debug_run(
        self, run_id: str, status: str, finished_at: str
    ) -> dict[str, Any]:
        if status not in {
            "SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "UNVERIFIED",
        }:
            raise PublishGateError("invalid debug status")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE strategy_debug_runs SET status = ?, finished_at = ? "
                "WHERE run_id = ? AND status = 'RUNNING'",
                (status, finished_at, run_id),
            )
            if cursor.rowcount != 1:
                raise PublishGateError("active debug run not found")
            return dict(
                connection.execute(
                    "SELECT * FROM strategy_debug_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def prepare_release(
        self,
        action_id: str,
        action_revision: int,
        actor: PublicationActor,
        *,
        waive_validation: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        actor = require_publication_actor(actor, require_admin=waive_validation)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            metadata, document = self._metadata_by_action_id(connection, action_id)
            if metadata["tombstoned_at"] is not None:
                raise ActionIdentityError("action identity is tombstoned")
            if metadata["action_revision"] != action_revision:
                raise PublishGateError("action revision is not current")
            validate_release_content(document)
            existing = connection.execute(
                "SELECT * FROM strategy_releases WHERE action_id = ? "
                "AND source_revision = ? AND content_checksum = ?",
                (action_id, action_revision, metadata["content_checksum"]),
            ).fetchone()
            if existing is not None:
                return self._strategy_release_record(existing)

            debug_run_id = None
            if waive_validation:
                if not isinstance(reason, str) or not reason.strip():
                    raise PublishGateError("waiver reason is required")
                validation_status = "waived"
            else:
                validation_status = "validated"
                debug = connection.execute(
                    "SELECT run_id FROM strategy_debug_runs WHERE action_id = ? "
                    "AND action_revision = ? AND content_checksum = ? AND status = 'SUCCEEDED' "
                    "ORDER BY finished_at DESC LIMIT 1",
                    (action_id, action_revision, metadata["content_checksum"]),
                ).fetchone()
                if debug is None:
                    raise PublishGateError("current action content has not passed local debug")
                debug_run_id = debug["run_id"]
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                "FROM strategy_releases WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            release_revision = int(row["revision"])
            digest = release_checksum(
                action_id, release_revision, metadata["content_checksum"]
            )
            release_document = {
                **document,
                "action_id": action_id,
                "revision": release_revision,
                "content_checksum": metadata["content_checksum"],
                "release_checksum": digest,
                "validation_status": validation_status,
                "actor": actor.actor_id,
                "waiver_reason": reason.strip() if waive_validation else "",
            }
            connection.execute(
                "INSERT INTO strategy_releases"
                "(action_id, revision, source_revision, content_checksum, release_checksum, "
                "validation_status, release_json, debug_run_id, actor, waiver_reason, "
                "central_revision, synced_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (
                    action_id,
                    release_revision,
                    action_revision,
                    metadata["content_checksum"],
                    digest,
                    validation_status,
                    _json(release_document),
                    debug_run_id,
                    actor.actor_id,
                    reason.strip() if waive_validation else "",
                    utc_now_iso(),
                ),
            )
            return self._strategy_release_record(
                connection.execute(
                    "SELECT * FROM strategy_releases WHERE action_id = ? AND revision = ?",
                    (action_id, release_revision),
                ).fetchone()
            )

    def mark_release_synced(
        self,
        action_id: str,
        revision: int,
        central_revision: int,
        synced_at: str,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT central_revision FROM strategy_releases "
                "WHERE action_id = ? AND revision = ?",
                (action_id, revision),
            ).fetchone()
            if existing is None:
                raise PublishGateError("release not found")
            if existing["central_revision"] not in {None, central_revision}:
                raise PublishGateError("central revision sync conflict")
            connection.execute(
                "UPDATE strategy_releases SET central_revision = ?, synced_at = ? "
                "WHERE action_id = ? AND revision = ?",
                (central_revision, synced_at, action_id, revision),
            )
        return self._strategy_release(action_id, revision)

    def _metadata_by_action_id(
        self,
        connection: sqlite3.Connection,
        action_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        identity = connection.execute(
            "SELECT * FROM strategy_action_identities WHERE action_id = ?",
            (str(action_id),),
        ).fetchone()
        if identity is None:
            raise ActionIdentityError("action identity not found")
        if identity["tombstoned_at"] is not None or identity["strategy_id"] is None:
            raise ActionIdentityError("action identity is tombstoned")
        row = connection.execute(
            "SELECT * FROM strategies WHERE id = ?", (identity["strategy_id"],)
        ).fetchone()
        if row is None:
            raise ActionIdentityError("action identity has no local strategy")
        return self._refresh_strategy_metadata(connection, identity, row)

    def _refresh_strategy_metadata(
        self,
        connection: sqlite3.Connection,
        identity: sqlite3.Row,
        row: sqlite3.Row,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        document = self._strategy_content_document(connection, row)
        digest = content_checksum(document)
        source_revision = int(identity["source_revision"])
        if not identity["content_checksum"]:
            connection.execute(
                "UPDATE strategy_action_identities SET content_checksum = ? WHERE action_id = ?",
                (digest, identity["action_id"]),
            )
        elif identity["content_checksum"] != digest:
            source_revision += 1
            connection.execute(
                "UPDATE strategy_action_identities SET source_revision = ?, content_checksum = ? "
                "WHERE action_id = ?",
                (source_revision, digest, identity["action_id"]),
            )
        return (
            {
                "local_id": row["id"],
                "action_id": identity["action_id"],
                "action_revision": source_revision,
                "executor_kind": "browser_strategy",
                "content_checksum": digest,
                "tombstoned_at": identity["tombstoned_at"],
            },
            document,
        )

    def _strategy_release(self, action_id: str, revision: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_releases WHERE action_id = ? AND revision = ?",
                (action_id, revision),
            ).fetchone()
        if row is None:
            raise PublishGateError("release not found")
        return self._strategy_release_record(row)

    @staticmethod
    def _strategy_release_record(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["release_payload"] = json.loads(result.pop("release_json"))
        return result

    @classmethod
    def _strategy_content_document(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        strategy = cls._strategy_record(connection, row)
        assert strategy is not None
        snapshot = {
            key: strategy[key]
            for key in (
                "target_url",
                "ready_element_id",
                "readiness_timeout_seconds",
                "run_mode",
                "loop_duration_minutes",
                "actions",
            )
        }
        element_ids = referenced_element_ids(snapshot)
        elements = []
        for element_id in element_ids:
            element = connection.execute(
                "SELECT id, purpose, kind, status, revision, definition_json "
                "FROM elements WHERE id = ?",
                (element_id,),
            ).fetchone()
            if element is None:
                raise StrategyValidationError(f"strategy references missing element: {element_id}")
            elements.append(
                {
                    "id": element["id"],
                    "purpose": element["purpose"],
                    "kind": element["kind"],
                    "status": element["status"],
                    "revision": element["revision"],
                    "definition": json.loads(element["definition_json"]),
                }
            )
        return {
            "executor_kind": "browser_strategy",
            "definition_schema_version": "1.0",
            "parameter_schema": {
                "type": "object",
                "properties": {
                    "target_url": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2048,
                        "format": "https-url",
                    }
                },
                "required": ["target_url"],
                "additionalProperties": False,
                "bindings": {
                    "target_url": {"pointer": "/strategy/target_url", "type": "string"}
                },
            },
            "result_schema": {"type": "object", "additionalProperties": True},
            "snapshot": {"strategy": snapshot, "elements": elements},
            "execution_defaults": {
                "readiness_timeout_seconds": strategy["readiness_timeout_seconds"]
            },
        }

    def build_execution_snapshot(
        self, strategy_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Read one immutable strategy-plus-elements execution view in one DB transaction."""

        if expected_revision is not None:
            self._strategy_expected_revision(expected_revision)
        with self.connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM strategies WHERE id = ?", (str(strategy_id),)
            ).fetchone()
            if row is None:
                raise StrategyNotFoundError(str(strategy_id))
            if expected_revision is not None and row["revision"] != expected_revision:
                raise StrategyRevisionConflictError(
                    f"strategy {strategy_id} revision conflict: expected {expected_revision}"
                )
            strategy = self._strategy_record(connection, row)
            identifiers = referenced_element_ids(strategy)
            if not identifiers:
                elements: list[dict[str, Any]] = []
            else:
                placeholders = ", ".join("?" for _ in identifiers)
                rows = connection.execute(
                    f"SELECT * FROM elements WHERE id IN ({placeholders})", identifiers
                ).fetchall()
                records = {row["id"]: self._element_record(row) for row in rows}
                if set(records) != set(identifiers):
                    missing = next(identifier for identifier in identifiers if identifier not in records)
                    raise StrategyValidationError(f"strategy references missing element: {missing}")
                elements = [records[identifier] for identifier in identifiers]
            return {"strategy": strategy, "elements": elements}

    @staticmethod
    def _split_strategy_definition(definition: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        stored = dict(definition)
        actions = stored.pop("actions")
        return stored, actions

    @staticmethod
    def _strategy_actions(connection: sqlite3.Connection, strategy_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT position, action_json FROM strategy_actions WHERE strategy_id = ? ORDER BY position ASC",
            (strategy_id,),
        ).fetchall()
        actions = []
        for row in rows:
            action = json.loads(row["action_json"])
            action["position"] = row["position"]
            actions.append(action)
        return actions

    @classmethod
    def _strategy_record(
        cls, connection: sqlite3.Connection, row: sqlite3.Row | None
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        definition = json.loads(record.pop("definition_json"))
        actions = cls._strategy_actions(connection, record["id"])
        for action in actions:
            action.pop("position")
        record.update(definition)
        record["actions"] = actions
        record["enabled"] = bool(record["enabled"])
        return record

    @staticmethod
    def _elements_by_id(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        rows = connection.execute("SELECT id, purpose, kind, status FROM elements").fetchall()
        return {row["id"]: dict(row) for row in rows}

    @staticmethod
    def _insert_strategy_actions(
        connection: sqlite3.Connection, strategy_id: str, actions: list[dict[str, Any]]
    ) -> None:
        connection.executemany(
            "INSERT INTO strategy_actions (strategy_id, position, action_json) VALUES (?, ?, ?)",
            [
                (strategy_id, position, _json(action))
                for position, action in enumerate(actions, start=1)
            ],
        )

    @staticmethod
    def _validate_strategy_id(strategy_id: str) -> None:
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise StrategyValidationError("strategy_id must be a non-empty string")

    @staticmethod
    def _validate_strategy_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise StrategyValidationError("strategy name must be a non-empty string")

    @staticmethod
    def _validate_enabled(enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise StrategyValidationError("enabled must be a boolean")

    @staticmethod
    def _strategy_expected_revision(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise StrategyValidationError("expected_revision must be a positive integer")
        return value

    @staticmethod
    def _raise_strategy_on_missing_or_conflict(
        connection: sqlite3.Connection, strategy_id: str, rowcount: int
    ) -> None:
        if rowcount:
            return
        exists = connection.execute(
            "SELECT 1 FROM strategies WHERE id = ?", (str(strategy_id),)
        ).fetchone()
        if exists is None:
            raise StrategyNotFoundError(str(strategy_id))
        raise StrategyRevisionConflictError(f"strategy {strategy_id} revision conflict")

    def create_job(
        self,
        job_id: str,
        strategy_id: str,
        strategy_snapshot: dict[str, Any],
        profile_ids: list[str],
        batch_size: int,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_jobs
                    (id, strategy_id, status, batch_size, strategy_snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job_id),
                    str(strategy_id),
                    JobStatus.QUEUED.value,
                    int(batch_size),
                    _json(strategy_snapshot),
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO execution_profiles
                    (job_id, profile_id, position, status, stage)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(job_id),
                        str(profile_id),
                        position,
                        ProfileStatus.QUEUED.value,
                        "",
                    )
                    for position, profile_id in enumerate(profile_ids, start=1)
                ],
            )
        return self.get_job_or_raise(job_id)

    def prepare_job(
        self,
        job_id: str,
        strategy_id: str,
        strategy_snapshot: dict[str, Any],
        profile_ids: list[str],
        batch_size: int,
    ) -> dict[str, Any]:
        """Persist one queued job before its background worker starts."""
        return self.create_job(
            job_id, strategy_id, strategy_snapshot, profile_ids, batch_size
        )

    def claim_queued_job(self, job_id: str) -> bool:
        """Atomically move a queued job to running. Only one worker can win."""
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE execution_jobs
                SET status = ?, started_at = COALESCE(started_at, ?)
                WHERE id = ? AND status = ?
                """,
                (JobStatus.RUNNING.value, now, str(job_id), JobStatus.QUEUED.value),
            )
        return cursor.rowcount == 1

    def set_job_status(self, job_id: str, status: JobStatus | str) -> None:
        value = _value(status)
        now = utc_now_iso()
        with self.connect() as connection:
            if value == JobStatus.RUNNING.value:
                connection.execute(
                    """
                    UPDATE execution_jobs
                    SET status = ?, started_at = COALESCE(started_at, ?)
                    WHERE id = ?
                    """,
                    (value, now, str(job_id)),
                )
            elif value in _TERMINAL_JOB_STATUSES:
                connection.execute(
                    "UPDATE execution_jobs SET status = ?, finished_at = ? WHERE id = ?",
                    (value, now, str(job_id)),
                )
            else:
                connection.execute(
                    "UPDATE execution_jobs SET status = ? WHERE id = ?",
                    (value, str(job_id)),
                )

    def set_profile_status(
        self,
        job_id: str,
        profile_id: str,
        status: ProfileStatus | str,
        stage: Stage | str,
        *,
        error_code: str = "",
        error_summary: str = "",
        close_confirmed: bool | None = None,
    ) -> None:
        status_value = _value(status)
        stage_value = _value(stage)
        now = utc_now_iso()
        assignments = [
            "status = ?",
            "stage = ?",
            "error_code = ?",
            "error_summary = ?",
        ]
        values: list[Any] = [
            status_value,
            stage_value,
            str(error_code),
            str(error_summary),
        ]
        if status_value != ProfileStatus.QUEUED.value:
            assignments.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status_value in _TERMINAL_PROFILE_STATUSES:
            assignments.append("finished_at = ?")
            values.append(now)
        if close_confirmed is not None:
            assignments.append("close_confirmed = ?")
            values.append(int(close_confirmed))
        values.extend((str(job_id), str(profile_id)))
        with self.connect() as connection:
            connection.execute(
                f"UPDATE execution_profiles SET {', '.join(assignments)} "
                "WHERE job_id = ? AND profile_id = ?",
                values,
            )

    def append_action_result(
        self,
        job_id: str,
        profile_id: str,
        action_index: int,
        action_type: str,
        status: str,
        stage: Stage | str,
        result: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO action_results
                    (job_id, profile_id, action_index, action_type, status, stage, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job_id),
                    str(profile_id),
                    int(action_index),
                    str(action_type),
                    str(status),
                    _value(stage),
                    _json(result),
                    utc_now_iso(),
                ),
            )

    def request_cancel(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE execution_jobs SET cancel_requested = 1 WHERE id = ?",
                (str(job_id),),
            )

    def is_cancel_requested(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM execution_jobs WHERE id = ?",
                (str(job_id),),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            return None
        return self._job_record(row)

    def get_job_or_raise(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"execution job not found: {job_id}")
        return job

    def list_jobs(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        self._validate_page(limit, offset)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_jobs
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._job_record(row) for row in rows]

    def list_recoverable_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_jobs
                WHERE status IN (?, ?)
                ORDER BY created_at ASC, id ASC
                """,
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchall()
        return [self._job_record(row) for row in rows]

    def list_profile_results(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_profiles
                WHERE job_id = ?
                ORDER BY position ASC
                """,
                (str(job_id),),
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["close_confirmed"] = bool(result["close_confirmed"])
            results.append(result)
        return results

    def list_action_results(
        self, job_id: str, *, profile_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT results.* FROM action_results AS results
            INNER JOIN execution_profiles AS profiles
              ON profiles.job_id = results.job_id
             AND profiles.profile_id = results.profile_id
            WHERE results.job_id = ?
        """
        values: list[Any] = [str(job_id)]
        if profile_id is not None:
            query += " AND results.profile_id = ?"
            values.append(str(profile_id))
        query += " ORDER BY profiles.position ASC, results.action_index ASC, results.id ASC"
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["result"] = json.loads(record.pop("result_json"))
            records.append(record)
        return records

    @staticmethod
    def _job_record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["strategy_snapshot"] = json.loads(record.pop("strategy_snapshot_json"))
        record["cancel_requested"] = bool(record["cancel_requested"])
        return record

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
