"""Transactional Outbox (PRD F12).

Business write + outbox insert happen in the same SQL transaction; a relay
process publishes pending messages and marks them sent. v1 ships a logging
relay; the NATS relay replaces it in M2.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, Session

from central.models import Base, utcnow

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"


class OutboxMessage(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=OUTBOX_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def add_outbox(
    session: Session,
    *,
    tenant_id: str,
    aggregate: str,
    subject: str,
    payload: dict,
) -> OutboxMessage:
    message = OutboxMessage(
        tenant_id=tenant_id,
        aggregate=aggregate,
        subject=subject,
        payload=payload,
    )
    session.add(message)
    return message


def claim_batch(session: Session, *, limit: int = 100, now: datetime | None = None) -> list[OutboxMessage]:
    now = now or datetime.now(timezone.utc)
    return (
        session.query(OutboxMessage)
        .filter(
            OutboxMessage.status == OUTBOX_PENDING,
            (OutboxMessage.next_attempt_at.is_(None))
            | (OutboxMessage.next_attempt_at <= now),
        )
        .order_by(OutboxMessage.id)
        .limit(limit)
        .all()
    )


def mark_sent(session: Session, message: OutboxMessage, now: datetime | None = None) -> None:
    message.status = OUTBOX_SENT
    message.published_at = now or datetime.now(timezone.utc)


def mark_failed(session: Session, message: OutboxMessage, *, retry_delay_seconds: int = 5) -> None:
    from datetime import timedelta

    message.attempts += 1
    message.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)
