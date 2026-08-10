"""Central tenant context and permission guards.

M1: tenant context is taken from the X-Tenant-ID header and enforced in the
data access layer via the get_session dependency. JWT authentication arrives
in M3; until then every request must carry the header explicitly.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from central import config
from central.permissions import has_permission

TENANT_HEADER = "X-Tenant-ID"


def require_tenant(x_tenant_id: str | None = Header(default=None)) -> str:
    tenant_id = x_tenant_id or config.DEFAULT_TENANT_ID
    tenant_id = tenant_id.strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant id is required")
    if len(tenant_id) > 64:
        raise HTTPException(status_code=400, detail="tenant id too long")
    return tenant_id


def require_permission(tenant_id: str, role: str, permission: str) -> None:
    if not has_permission(role, permission):
        raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
