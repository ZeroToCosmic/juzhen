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

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from central.models import Base

_EXCLUDED_TABLES = frozenset({"sqlite_sequence"})


def migrate(
    source: Engine,
    target: Engine,
    *,
    tables: list[str] | None = None,
    batch_size: int = 500,
) -> dict:
    Base.metadata.create_all(target)
    inspector = inspect(source)
    names = [name for name in inspector.get_table_names() if name not in _EXCLUDED_TABLES]
    if tables is not None:
        names = [name for name in names if name in set(tables)]

    counts: dict[str, int] = {}
    with source.connect() as source_conn, target.begin() as target_conn:
        for name in names:
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
                batch = [
                    {column: row[column] for column in columns}
                    for row in rows[start : start + batch_size]
                ]
                target_conn.execute(text(insert_sql), batch)
            counts[name] = len(rows)
    return counts
