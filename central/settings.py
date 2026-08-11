"""Versioned system configuration (PRD F6, M3).

Semantics: tenant_id "" means global; tenant config overrides global for
the tenant's effective view. Every write bumps version and appends a
ConfigVersion history row in the same transaction. Running tasks use the
frozen config_snapshot (created at task creation), so config changes do
not affect in-flight work; hot runtime parameters are out of scope for
now and would ride event push in a later increment.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from central.db import get_session
from central.models import ConfigSetting, ConfigVersion
from central.security import require_tenant

router = APIRouter(prefix="/api/central/configs", tags=["configs"])

GLOBAL_TENANT = ""


def _upsert(session: Session, *, tenant_id: str, key: str, value: dict, gray_ratio: float) -> ConfigSetting:
    setting = (
        session.query(ConfigSetting)
        .filter(ConfigSetting.tenant_id == tenant_id, ConfigSetting.key == key)
        .one_or_none()
    )
    now = datetime.now(timezone.utc)
    if setting is None:
        setting = ConfigSetting(
            tenant_id=tenant_id,
            key=key,
            value=value,
            version=1,
            gray_ratio=gray_ratio,
            effective_at=now,
            updated_at=now,
        )
        session.add(setting)
    else:
        session.add(
            ConfigVersion(
                tenant_id=tenant_id,
                key=key,
                value=setting.value,
                version=setting.version,
                created_at=now,
            )
        )
        setting.value = value
        setting.version += 1
        setting.gray_ratio = gray_ratio
        setting.updated_at = now
    return setting


class ConfigWriteRequest(BaseModel):
    value: dict = Field(default_factory=dict)
    gray_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    scope: str = Field(default="tenant", pattern="^(global|tenant)$")


def _effective(session: Session, tenant_id: str, key: str) -> ConfigSetting | None:
    tenant_setting = (
        session.query(ConfigSetting)
        .filter(ConfigSetting.tenant_id == tenant_id, ConfigSetting.key == key)
        .one_or_none()
    )
    if tenant_setting is not None:
        return tenant_setting
    return (
        session.query(ConfigSetting)
        .filter(ConfigSetting.tenant_id == GLOBAL_TENANT, ConfigSetting.key == key)
        .one_or_none()
    )


@router.get("")
def list_effective_configs(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    tenant_rows = (
        session.query(ConfigSetting).filter(ConfigSetting.tenant_id == tenant_id).all()
    )
    global_rows = (
        session.query(ConfigSetting).filter(ConfigSetting.tenant_id == GLOBAL_TENANT).all()
    )
    merged = {row.key: row for row in global_rows}
    merged.update({row.key: row for row in tenant_rows})
    return {
        "count": len(merged),
        "configs": [
            {
                "key": row.key,
                "value": row.value,
                "version": row.version,
                "gray_ratio": row.gray_ratio,
                "effective_at": row.effective_at.isoformat() if row.effective_at else None,
                "source": "tenant" if row.tenant_id == tenant_id else "global",
            }
            for row in merged.values()
        ],
    }


@router.get("/{key}")
def get_config(
    key: str,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    setting = _effective(session, tenant_id, key)
    if setting is None:
        raise HTTPException(status_code=404, detail="config not found")
    return {
        "key": setting.key,
        "value": setting.value,
        "version": setting.version,
        "gray_ratio": setting.gray_ratio,
        "effective_at": setting.effective_at.isoformat() if setting.effective_at else None,
        "source": "tenant" if setting.tenant_id == tenant_id else "global",
    }


@router.put("/{key}")
def put_config(
    key: str,
    payload: ConfigWriteRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    setting = _upsert(
        session,
        tenant_id=GLOBAL_TENANT if payload.scope == "global" else tenant_id,
        key=key,
        value=payload.value,
        gray_ratio=payload.gray_ratio,
    )
    return {
        "key": setting.key,
        "version": setting.version,
        "gray_ratio": setting.gray_ratio,
    }
