"""Tenant-scoped immutable action publication and inventory comparison API."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from central.action_models import ActionDefinition, ActionReleaseAuditEvent, ActionRevision
from central.db import get_session
from central.models import utcnow
from central.permissions import ROLE_ADMIN
from central.security import ActorContext, require_actor_context, require_tenant
from remote_actions.checksums import ChecksumError, content_checksum, release_checksum
from remote_actions.contracts import (
    ContractDecodeError,
    ContractSemanticError,
    validate_release_content,
)
from remote_actions.identifiers import validate_action_id


router = APIRouter(prefix="/api/central/actions", tags=["actions"])

class ActionRevisionPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_kind: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    definition_schema_version: str = Field(min_length=1, max_length=32)
    parameter_schema: dict[str, Any]
    result_schema: dict[str, Any]
    snapshot: dict[str, Any]
    execution_defaults: dict[str, Any]
    content_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", max_length=71)
    release_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", max_length=71)
    validation_status: Literal["validated", "waived"]
    actor: str = Field(min_length=1, max_length=120)
    waiver_reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_waiver(self):
        self.actor = self.actor.strip()
        self.waiver_reason = self.waiver_reason.strip()
        if not self.actor:
            raise ValueError("release actor is required")
        if self.validation_status == "waived" and not self.waiver_reason:
            raise ValueError("waiver reason is required")
        if self.validation_status == "validated" and self.waiver_reason:
            raise ValueError("validated release cannot include a waiver reason")
        return self


class InventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(pattern=r"^act_[0-7][0-9A-HJKMNP-TV-Z]{25}$", max_length=30)
    revision: int = Field(ge=1, le=9007199254740991)
    release_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", max_length=71)


class InventoryCompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inventory: list[InventoryItem] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique_items(self):
        keys = [(item.action_id, item.revision) for item in self.inventory]
        if len(keys) != len(set(keys)):
            raise ValueError("inventory contains duplicate action revisions")
        return self


def _validate_identity(action_id: str, revision: int) -> None:
    try:
        validate_action_id(action_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid action_id")
    if revision < 1 or revision > 9007199254740991:
        raise HTTPException(status_code=422, detail="invalid revision")


def _content(payload: ActionRevisionPut) -> dict[str, Any]:
    return {
        "executor_kind": payload.executor_kind,
        "definition_schema_version": payload.definition_schema_version,
        "parameter_schema": payload.parameter_schema,
        "result_schema": payload.result_schema,
        "snapshot": payload.snapshot,
        "execution_defaults": payload.execution_defaults,
    }


def _view(record: ActionRevision, definition: ActionDefinition) -> dict[str, Any]:
    return {
        "record_id": record.id,
        "tenant_id": definition.tenant_id,
        "action_id": record.action_id,
        "revision": record.revision,
        "executor_kind": definition.executor_kind,
        "content_checksum": record.content_checksum,
        "release_checksum": record.release_checksum,
        "definition_schema_version": record.definition_schema_version,
        "parameter_schema": record.parameter_schema,
        "result_schema": record.result_schema,
        "snapshot": record.snapshot,
        "execution_defaults": record.execution_defaults,
        "validation_status": record.validation_status,
        "actor": record.actor,
        "waiver_reason": record.waiver_reason,
        "created_at": record.created_at,
    }


def _insert_do_nothing(session: Session, model, values: dict[str, Any]) -> bool:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
    elif dialect == "postgresql":
        statement = postgresql_insert(model).values(**values).on_conflict_do_nothing()
    else:
        try:
            with session.begin_nested():
                session.execute(insert(model).values(**values))
            return True
        except IntegrityError:
            return False
    result = session.execute(statement)
    return result.rowcount == 1


@router.put("/{action_id}/revisions/{revision}")
def put_action_revision(
    action_id: str,
    revision: int,
    payload: ActionRevisionPut,
    tenant_id: str = Depends(require_tenant),
    actor_context: ActorContext = Depends(require_actor_context),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _validate_identity(action_id, revision)
    if payload.actor != actor_context.actor_id:
        raise HTTPException(status_code=403, detail="release actor does not match caller")
    if payload.validation_status == "waived" and actor_context.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="admin role is required for waiver")
    content = _content(payload)
    try:
        validate_release_content(content)
        expected_content = content_checksum(content)
        expected_release = release_checksum(action_id, revision, expected_content)
    except (ChecksumError, ContractDecodeError, ContractSemanticError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.content_checksum != expected_content or payload.release_checksum != expected_release:
        raise HTTPException(status_code=422, detail="action checksum mismatch")

    _insert_do_nothing(
        session,
        ActionDefinition,
        {
            "action_id": action_id,
            "tenant_id": tenant_id,
            "executor_kind": payload.executor_kind,
            "created_at": utcnow(),
        },
    )
    definition = session.get(ActionDefinition, action_id, populate_existing=True)
    if definition is None or (
        definition.tenant_id != tenant_id
        or definition.executor_kind != payload.executor_kind
    ):
        raise HTTPException(status_code=409, detail="action identity conflict")
    release_payload = {
        **content,
        "action_id": action_id,
        "revision": revision,
        "content_checksum": payload.content_checksum,
        "release_checksum": payload.release_checksum,
        "validation_status": payload.validation_status,
        "actor": payload.actor,
        "waiver_reason": payload.waiver_reason,
    }
    inserted = _insert_do_nothing(
        session,
        ActionRevision,
        {
            "action_id": action_id,
            "revision": revision,
            "content_checksum": payload.content_checksum,
            "release_checksum": payload.release_checksum,
            "definition_schema_version": payload.definition_schema_version,
            "parameter_schema": payload.parameter_schema,
            "result_schema": payload.result_schema,
            "snapshot": payload.snapshot,
            "execution_defaults": payload.execution_defaults,
            "validation_status": payload.validation_status,
            "actor": payload.actor,
            "waiver_reason": payload.waiver_reason,
            "release_payload": release_payload,
            "created_at": utcnow(),
        },
    )
    record = (
        session.query(ActionRevision)
        .filter(
            ActionRevision.action_id == action_id,
            ActionRevision.revision == revision,
        )
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status_code=409, detail="release checksum conflict")
    if record.release_payload != release_payload:
        raise HTTPException(status_code=409, detail="immutable revision conflict")
    if inserted and payload.validation_status == "waived":
        session.add(
            ActionReleaseAuditEvent(
                tenant_id=tenant_id,
                action_id=action_id,
                revision=revision,
                event_type="validation_waived",
                actor=actor_context.actor_id,
                actor_role=actor_context.role,
                reason=payload.waiver_reason,
            )
        )
        session.flush()
    return _view(record, definition)


@router.get("/{action_id}/revisions/{revision}")
def get_action_revision(
    action_id: str,
    revision: int,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _validate_identity(action_id, revision)
    definition = session.get(ActionDefinition, action_id)
    if definition is None or definition.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="action revision not found")
    record = (
        session.query(ActionRevision)
        .filter(
            ActionRevision.action_id == action_id,
            ActionRevision.revision == revision,
        )
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="action revision not found")
    return _view(record, definition)


@router.post("/sync/compare")
def compare_action_inventory(
    payload: InventoryCompareRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    remote_rows = (
        session.query(ActionRevision, ActionDefinition)
        .join(ActionDefinition, ActionDefinition.action_id == ActionRevision.action_id)
        .filter(ActionDefinition.tenant_id == tenant_id)
        .all()
    )
    remote = {
        (revision.action_id, revision.revision): revision.release_checksum
        for revision, _definition in remote_rows
    }
    local = {
        (item.action_id, item.revision): item.release_checksum
        for item in payload.inventory
    }
    items = []
    for action_id, revision in sorted(set(remote) | set(local)):
        key = (action_id, revision)
        if key not in remote:
            state = "missing_remote"
        elif key not in local:
            state = "missing_local"
        elif remote[key] != local[key]:
            state = "checksum_mismatch"
        else:
            state = "in_sync"
        items.append(
            {
                "action_id": action_id,
                "revision": revision,
                "state": state,
                "local_release_checksum": local.get(key),
                "remote_release_checksum": remote.get(key),
            }
        )
    return {"items": items}
