"""Consumer-side Inbox deduplication (PRD F12).

The consumer inserts the (msg_id, subject) row in the same transaction as the
business write; a second delivery of the same message is rejected. The DB
commit doubles as the message ack.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, Session

from central.models import Base, utcnow


class InboxMessage(Base):
    __tablename__ = "inbox"

    msg_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str] = mapped_column(String(256), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def try_dedupe(session: Session, *, msg_id: str, subject: str, payload: dict) -> bool:
    existing = session.get(InboxMessage, (msg_id, subject))
    if existing is not None:
        return False
    session.add(InboxMessage(msg_id=msg_id, subject=subject, payload=payload))
    return True
