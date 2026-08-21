"""Outbox relay (M1: logging publisher; M2: NATS JetStream).

The relay claims pending outbox rows, publishes each payload, and marks the
row sent in the same transaction as the publish acknowledgement. A failed
publish marks the row failed with backoff instead of losing it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from central import outbox

Publisher = Callable[[str, str, dict], None]


@dataclass
class LoggingPublisher:
    log: Callable[[str], None] = print

    def __call__(self, tenant_id: str, subject: str, payload: dict) -> None:
        self.log(f"[outbox] publish tenant={tenant_id} subject={subject}")


def relay_pending(
    session: Session,
    publisher: Publisher,
    *,
    limit: int = 100,
) -> dict:
    messages = outbox.claim_batch(session, limit=limit)
    published = 0
    failed = 0
    for message in messages:
        try:
            publisher(message.tenant_id, message.subject, message.payload)
        except BaseException:
            outbox.mark_failed(session, message)
            failed += 1
        else:
            outbox.mark_sent(session, message)
            published += 1
    return {"claimed": len(messages), "published": published, "failed": failed}
