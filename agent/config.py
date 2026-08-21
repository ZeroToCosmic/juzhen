"""Agent-side configuration (M2 increment 3)."""

from __future__ import annotations

import os

CENTRAL_BASE_URL = os.getenv("CENTRAL_BASE_URL", "http://127.0.0.1:8000")
TENANT_ID = os.getenv("AGENT_TENANT_ID", "tenant-default")
DEVICE_ID = os.getenv("AGENT_DEVICE_ID", "")
SESSION_ID = os.getenv("AGENT_SESSION_ID", "")
AGENT_VERSION = os.getenv("AGENT_VERSION", "0.1.0")
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("AGENT_HEARTBEAT_INTERVAL", "30"))
RENEW_INTERVAL_SECONDS = int(os.getenv("AGENT_RENEW_INTERVAL", "60"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("AGENT_REQUEST_TIMEOUT", "15"))
MAX_CONCURRENT_WINDOWS = int(os.getenv("AGENT_MAX_CONCURRENT_WINDOWS", "3"))
