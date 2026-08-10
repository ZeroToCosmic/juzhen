"""Central outbox/inbox tests (M1): transactional semantics + relay."""

from __future__ import annotations

import pytest

from central import config, db
from central.inbox import InboxMessage, try_dedupe
from central.models import Base
from central.outbox import OutboxMessage, add_outbox, claim_batch, mark_sent
from central.relay import LoggingPublisher, relay_pending


@pytest.fixture()
def central_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CENTRAL_DB_PATH", tmp_path / "central.db")
    db._engine = None
    db._session_factory = None
    Base.metadata.create_all(db.get_engine())


def test_outbox_write_rolls_back_with_business_write(central_db):
    with pytest.raises(RuntimeError):
        with db.session_scope() as session:
            add_outbox(
                session,
                tenant_id="tenant-a",
                aggregate="subtask",
                subject="tenant-a/subtask.assigned",
                payload={"subtask_id": "st-1"},
            )
            raise RuntimeError("business write failed")
    with db.session_scope() as session:
        assert session.query(OutboxMessage).count() == 0


def test_outbox_write_commits_with_business_write(central_db):
    with db.session_scope() as session:
        add_outbox(
            session,
            tenant_id="tenant-a",
            aggregate="subtask",
            subject="tenant-a/subtask.assigned",
            payload={"subtask_id": "st-1"},
        )
    with db.session_scope() as session:
        messages = session.query(OutboxMessage).all()
        assert len(messages) == 1
        assert messages[0].status == "pending"
        assert messages[0].payload == {"subtask_id": "st-1"}


def test_relay_publishes_and_marks_sent(central_db):
    with db.session_scope() as session:
        add_outbox(
            session,
            tenant_id="tenant-a",
            aggregate="subtask",
            subject="tenant-a/subtask.assigned",
            payload={"subtask_id": "st-1"},
        )
    published = []

    def publisher(tenant_id, subject, payload):
        published.append((tenant_id, subject, payload))

    with db.session_scope() as session:
        result = relay_pending(session, publisher)
    assert result == {"claimed": 1, "published": 1, "failed": 0}
    assert published == [("tenant-a", "tenant-a/subtask.assigned", {"subtask_id": "st-1"})]
    with db.session_scope() as session:
        message = session.query(OutboxMessage).one()
        assert message.status == "sent"
        assert message.published_at is not None


def test_relay_retries_failed_publish(central_db):
    with db.session_scope() as session:
        add_outbox(
            session,
            tenant_id="tenant-a",
            aggregate="subtask",
            subject="tenant-a/subtask.assigned",
            payload={},
        )

    def failing_publisher(tenant_id, subject, payload):
        raise RuntimeError("nats down")

    with db.session_scope() as session:
        result = relay_pending(session, failing_publisher)
    assert result == {"claimed": 1, "published": 0, "failed": 1}
    with db.session_scope() as session:
        message = session.query(OutboxMessage).one()
        assert message.status == "pending"
        assert message.attempts == 1
        assert message.next_attempt_at is not None


def test_relay_skips_backoff_messages(central_db):
    with db.session_scope() as session:
        add_outbox(
            session,
            tenant_id="tenant-a",
            aggregate="subtask",
            subject="a/1",
            payload={},
        )
        add_outbox(
            session,
            tenant_id="tenant-a",
            aggregate="subtask",
            subject="a/2",
            payload={},
        )
    with db.session_scope() as session:
        session.query(OutboxMessage).filter(OutboxMessage.subject == "a/2").one()
    from datetime import datetime, timedelta, timezone

    with db.session_scope() as session:
        second = (
            session.query(OutboxMessage).filter(OutboxMessage.subject == "a/2").one()
        )
        second.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=60)

    claimed_subjects = []
    with db.session_scope() as session:
        for message in claim_batch(session, limit=10):
            claimed_subjects.append(message.subject)
    assert claimed_subjects == ["a/1"]


def test_inbox_dedupe_rejects_duplicate(central_db):
    with db.session_scope() as session:
        first = try_dedupe(
            session,
            msg_id="msg-1",
            subject="tenant-a/subtask.result",
            payload={"subtask_id": "st-1"},
        )
    assert first is True
    with db.session_scope() as session:
        second = try_dedupe(
            session,
            msg_id="msg-1",
            subject="tenant-a/subtask.result",
            payload={"subtask_id": "st-1"},
        )
    assert second is False
    with db.session_scope() as session:
        assert session.query(InboxMessage).count() == 1


def test_inbox_same_msg_id_different_subject_allowed(central_db):
    with db.session_scope() as session:
        assert try_dedupe(session, msg_id="msg-1", subject="a/1", payload={}) is True
        assert try_dedupe(session, msg_id="msg-1", subject="a/2", payload={}) is True
    with db.session_scope() as session:
        assert session.query(InboxMessage).count() == 2
