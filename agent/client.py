"""Agent -> Central HTTP client (M2 increment 3)."""

from __future__ import annotations

import requests

from agent import config


class CentralError(RuntimeError):
    pass


class CentralClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        tenant_id: str | None = None,
        device_id: str | None = None,
        session_id: str | None = None,
        agent_version: str | None = None,
        timeout: float | None = None,
    ):
        self.base_url = (base_url or config.CENTRAL_BASE_URL).rstrip("/")
        self.tenant_id = tenant_id or config.TENANT_ID
        self.device_id = device_id or config.DEVICE_ID
        self.session_id = session_id or config.SESSION_ID
        self.agent_version = agent_version or config.AGENT_VERSION
        self.timeout = timeout or config.REQUEST_TIMEOUT_SECONDS

    def _headers(self) -> dict:
        return {"X-Tenant-ID": self.tenant_id}

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise CentralError(f"central unreachable: {error}") from error
        if response.status_code >= 400:
            detail = response.json().get("detail", "") if response.headers.get("content-type", "").startswith("application/json") else ""
            raise CentralError(f"central {response.status_code}: {detail}")
        return response.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise CentralError(f"central unreachable: {error}") from error
        if response.status_code >= 400:
            raise CentralError(f"central {response.status_code}")
        return response.json()

    def heartbeat(
        self,
        *,
        capabilities: dict,
        max_accounts: int = 300,
        used_accounts: int = 0,
        inventory_epoch: int = 0,
        running_windows: int = 0,
        queue_depth: int = 0,
        channel: str = "stable",
    ) -> dict:
        return self._post(
            "/api/central/devices/heartbeat",
            {
                "tenant_id": self.tenant_id,
                "device_id": self.device_id,
                "session_id": self.session_id,
                "agent_version": self.agent_version,
                "capabilities": capabilities,
                "channel": channel,
                "max_accounts": max_accounts,
                "used_accounts": used_accounts,
                "inventory_epoch": inventory_epoch,
                "running_windows": running_windows,
                "queue_depth": queue_depth,
            },
        )

    def pull_subtasks(self) -> list[dict]:
        body = self._get(
            "/api/central/agent/subtasks",
            params={"device_id": self.device_id},
        )
        return body.get("subtasks", [])

    def renew_lease(self, subtask_id: str, generation: int) -> dict:
        return self._post(
            "/api/central/subtasks/lease/renew",
            {
                "subtask_id": subtask_id,
                "device_id": self.device_id,
                "generation": generation,
            },
        )

    def submit_result(
        self,
        *,
        subtask_id: str,
        generation: int,
        status: str,
        error_category: str = "",
        error_code: str = "",
        result_data: dict | None = None,
        duration_ms: int = 0,
        msg_id: str,
    ) -> dict:
        return self._post(
            "/api/central/subtasks/result",
            {
                "subtask_id": subtask_id,
                "device_id": self.device_id,
                "generation": generation,
                "status": status,
                "error_category": error_category,
                "error_code": error_code,
                "result_data": result_data or {},
                "duration_ms": duration_ms,
                "msg_id": msg_id,
            },
        )

    def submit_handle(
        self,
        *,
        subtask_id: str,
        verification_status: str,
        content: dict | None = None,
        text_hash: str = "",
    ) -> dict:
        return self._post(
            "/api/central/subtasks/handle",
            {
                "subtask_id": subtask_id,
                "verification_status": verification_status,
                "content": content or {},
                "text_hash": text_hash,
            },
        )
