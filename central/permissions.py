"""Central permission points and role mapping (PRD section 3)."""

from __future__ import annotations

ROLE_ADMIN = "administrator"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

PERMISSIONS = frozenset(
    {
        "task:create",
        "task:cancel",
        "account:manage",
        "device:view",
        "device:manage",
        "config:edit",
        "review:handle",
        "strategy:manage",
        "tenant:manage",
        "upgrade:publish",
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: PERMISSIONS,
    ROLE_OPERATOR: frozenset(
        {
            "task:create",
            "task:cancel",
            "account:manage",
            "device:view",
            "review:handle",
        }
    ),
    ROLE_VIEWER: frozenset({"task:create", "device:view"}),
}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
