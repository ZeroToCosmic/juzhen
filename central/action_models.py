"""Immutable action definitions and revisions owned by Central."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from central.models import Base, utcnow


_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")


def _checksum_sql(column: str) -> str:
    remainder = f"substr({column}, 8)"
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = 71 AND substr({column}, 1, 7) = 'sha256:' "
        f"AND length({remainder}) = 0"
    )


def _action_id_sql(column: str) -> str:
    alphabet = ",".join(f"'{character}'" for character in "0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    tail_checks = " AND ".join(
        f"substr({column}, {position}, 1) IN ({alphabet})"
        for position in range(6, 31)
    )
    return (
        f"length({column}) = 30 AND substr({column}, 1, 4) = 'act_' "
        f"AND substr({column}, 5, 1) IN ('0','1','2','3','4','5','6','7') "
        f"AND {tail_checks}"
    )


class ActionDefinition(Base):
    __tablename__ = "action_definitions"
    __table_args__ = (
        CheckConstraint(_action_id_sql("action_id"), name="ck_action_definition_id"),
    )

    action_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, index=True
    )
    executor_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ActionRevision(Base):
    __tablename__ = "action_revisions"
    __table_args__ = (
        UniqueConstraint("action_id", "revision", name="uq_action_revision"),
        UniqueConstraint("release_checksum", name="uq_action_release_checksum"),
        CheckConstraint(
            _checksum_sql("content_checksum"),
            name="ck_action_content_checksum_shape",
        ),
        CheckConstraint(
            _checksum_sql("release_checksum"),
            name="ck_action_release_checksum_shape",
        ),
        CheckConstraint(
            "validation_status IN ('validated', 'waived')",
            name="ck_action_validation_status",
        ),
        CheckConstraint(
            "(validation_status = 'validated' AND waiver_reason = '') OR "
            "(validation_status = 'waived' AND length(trim(waiver_reason)) > 0)",
            name="ck_action_waiver_reason",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("action_definitions.action_id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    release_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    definition_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parameter_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution_defaults: Mapped[dict] = mapped_column(JSON, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    waiver_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    release_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ActionReleaseAuditEvent(Base):
    __tablename__ = "action_release_audit_events"
    __table_args__ = (
        UniqueConstraint(
            "action_id", "revision", "event_type", name="uq_action_release_audit_event"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, index=True
    )
    action_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("action_definitions.action_id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


@event.listens_for(ActionRevision, "before_insert")
def _validate_action_revision_checksums(_mapper, _connection, target) -> None:
    if not _CHECKSUM.fullmatch(target.content_checksum or ""):
        raise ValueError("invalid content checksum")
    if not _CHECKSUM.fullmatch(target.release_checksum or ""):
        raise ValueError("invalid release checksum")


@event.listens_for(ActionRevision, "before_update")
@event.listens_for(ActionRevision, "before_delete")
def _reject_action_revision_mutation(_mapper, _connection, _target) -> None:
    raise ValueError("published action revisions are immutable")


def install_action_definition_guards(connection) -> None:
    """Install idempotent guards for both new and pre-constraint databases."""

    if connection.dialect.name == "sqlite":
        condition = (
            "NEW.action_id IS NULL OR NOT ("
            "length(NEW.action_id) = 30 "
            "AND substr(NEW.action_id, 1, 4) = 'act_' "
            "AND substr(NEW.action_id, 5, 1) IN ('0','1','2','3','4','5','6','7') "
            "AND substr(NEW.action_id, 6) NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*')"
        )
        for operation in ("INSERT", "UPDATE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS trg_action_definitions_valid_{operation.lower()} "
                f"BEFORE {operation} ON action_definitions WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'invalid action_id'); END"
            )
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "CREATE OR REPLACE FUNCTION reject_invalid_action_definition_id() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.action_id IS NULL OR NEW.action_id !~ "
            "'^act_[0-7][0-9A-HJKMNP-TV-Z]{25}$' THEN "
            "RAISE EXCEPTION USING MESSAGE = 'invalid action_id', ERRCODE = '23514'; "
            "END IF; RETURN NEW; END; $$"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_action_definitions_valid_id ON action_definitions"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER trg_action_definitions_valid_id "
            "BEFORE INSERT OR UPDATE ON action_definitions FOR EACH ROW "
            "EXECUTE FUNCTION reject_invalid_action_definition_id()"
        )


@event.listens_for(ActionDefinition.__table__, "after_create")
def _install_new_action_definition_guards(_target, connection, **_kwargs) -> None:
    install_action_definition_guards(connection)


@event.listens_for(ActionRevision.__table__, "after_create")
def _install_action_revision_immutability(_target, connection, **_kwargs) -> None:
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS trg_action_revisions_no_update "
            "BEFORE UPDATE ON action_revisions BEGIN "
            "SELECT RAISE(ABORT, 'published action revisions are immutable'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS trg_action_revisions_no_delete "
            "BEFORE DELETE ON action_revisions BEGIN "
            "SELECT RAISE(ABORT, 'published action revisions are immutable'); END"
        )
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "CREATE OR REPLACE FUNCTION reject_action_revision_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION USING MESSAGE = 'published action revisions are immutable', "
            "ERRCODE = '23514'; END; $$"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER trg_action_revisions_no_update_delete "
            "BEFORE UPDATE OR DELETE ON action_revisions FOR EACH ROW "
            "EXECUTE FUNCTION reject_action_revision_mutation()"
        )
