"""Central database migration tool (PRD section 14 / ADR-0010).

Copies all central tables from a source SQLAlchemy URL (SQLite by
default) to a target URL (PostgreSQL in production, SQLite in tests).
The target schema is created from the central models metadata so JSON
columns map to the right target types; data rows are then copied
table-by-table. Migration is idempotent for fresh targets; reruns on an
existing target require the target to be empty (batch_id style
idempotency lives at the business layer).

Usage:
    python -m central.migrate --source sqlite:///data/central/central.db \
        --target postgresql+asyncmy://user:pass@host/central
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import JSON, Boolean, create_engine, inspect, text
from sqlalchemy.engine import Engine

from central.inbox import InboxMessage  # noqa: F401  (populates Base.metadata)
from central.action_models import ActionDefinition, ActionRevision  # noqa: F401
from central.models import Base
from central.outbox import OutboxMessage  # noqa: F401  (populates Base.metadata)

_EXCLUDED_TABLES = frozenset({"sqlite_sequence"})

_DEPENDENCY_ORDER = [
    "tenants",
    "users",
    "action_definitions",
    "action_revisions",
    "action_release_audit_events",
    "devices",
    "device_sessions",
    "accounts",
    "import_jobs",
    "tasks",
    "subtasks",
    "deploy_tasks",
    "dependency_edges",
    "handles",
    "task_results",
    "leases",
    "outbox",
    "inbox",
    "audit_events",
    "configs",
    "config_versions",
    "strategies",
    "agent_releases",
    "dlq_items",
    "account_status_logs",
]


def _ordered_table_names(inspector) -> list[str]:
    names = {
        name for name in inspector.get_table_names() if name not in _EXCLUDED_TABLES
    }
    ordered = [name for name in _DEPENDENCY_ORDER if name in names]
    remaining = sorted(names - set(ordered))
    return ordered + remaining


def migrate(
    source: Engine,
    target: Engine,
    *,
    tables: list[str] | None = None,
    batch_size: int = 500,
) -> dict:
    Base.metadata.create_all(target)
    inspector = inspect(source)
    names = _ordered_table_names(inspector)
    if tables is not None:
        names = [name for name in names if name in set(tables)]

    counts: dict[str, int] = {}
    with source.connect() as source_conn, target.begin() as target_conn:
        for name in names:
            columns_info = {
                column["name"]: column["type"] for column in inspector.get_columns(name)
            }
            rows = source_conn.execute(text(f'SELECT * FROM "{name}"')).mappings().all()
            if not rows:
                counts[name] = 0
                continue
            columns = list(rows[0].keys())
            placeholders = ", ".join(f":{column}" for column in columns)
            insert_sql = (
                f'INSERT INTO "{name}" ({", ".join(f'"{c}"' for c in columns)}) '
                f"VALUES ({placeholders})"
            )
            for start in range(0, len(rows), batch_size):
                batch = []
                for row in rows[start : start + batch_size]:
                    converted = {}
                    for column in columns:
                        value = row[column]
                        column_type = columns_info.get(column)
                        if isinstance(column_type, Boolean) and isinstance(value, int):
                            value = bool(value)
                        elif isinstance(column_type, JSON):
                            if isinstance(value, str):
                                value = json.loads(value)
                            if isinstance(value, (dict, list)):
                                value = json.dumps(value, ensure_ascii=False)
                        converted[column] = value
                    batch.append(converted)
                target_conn.execute(text(insert_sql), batch)
            counts[name] = len(rows)
    return counts
