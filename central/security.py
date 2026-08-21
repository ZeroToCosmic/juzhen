"""Central tenant context and permission guards.

M1: tenant context is taken from the X-Tenant-ID header and enforced in the
data access layer via the get_session dependency. JWT authentication arrives
in M3; until then every request must carry the header explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from central import config
from central.permissions import has_permission

TENANT_HEADER = "X-Tenant-ID"


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    role: str
    authenticated: bool = False


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


def require_actor_context(
    request: Request,
) -> ActorContext:
    """Require identity installed by trusted authentication middleware."""

    context = getattr(request.state, "actor_context", None)
    if not isinstance(context, ActorContext) or not context.authenticated:
        raise HTTPException(status_code=401, detail="authenticated actor is required")
    actor_id = context.actor_id.strip()
    role = context.role.strip()
    if not actor_id or len(actor_id) > 120 or not role or len(role) > 32:
        raise HTTPException(status_code=401, detail="invalid authenticated actor context")
    return ActorContext(actor_id=actor_id, role=role, authenticated=True)
