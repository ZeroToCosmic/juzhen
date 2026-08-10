"""Central control service for the business control system (M1 skeleton)."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CENTRAL_DB_PATH = Path(
    os.getenv("CENTRAL_DB_PATH", PROJECT_ROOT / "data" / "central" / "central.db")
)
DEFAULT_TENANT_ID = os.getenv("CENTRAL_DEFAULT_TENANT", "tenant-default")
HEARTBEAT_ONLINE_SECONDS = int(os.getenv("HEARTBEAT_ONLINE_SECONDS", "90"))
LEASE_TIMEOUT_SECONDS = int(os.getenv("LEASE_TIMEOUT_SECONDS", "300"))
MAX_RETRY_ATTEMPTS = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
