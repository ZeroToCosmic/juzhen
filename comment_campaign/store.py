"""Transactional persistence for Comment Campaigns.

Raw AdsPower identifiers are intentionally confined to the identity table and the
single internal lookup method below.  Every returned record is built explicitly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import case, delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError

from .database import create_campaign_engine, create_campaign_session_factory
from .allocation import profile_matches
from .domain import (
    AssignmentStatus,
    CampaignStatus,
    transition_assignment,
    transition_campaign,
)
from .errors import CampaignNotFoundError, CampaignValidationError, RevisionConflictError
from .models import (
    Base,
    CommentAssignmentRecord,
    CommentApprovalRecord,
    CommentAttemptRecord,
    CommentCampaignRecord,
    CommentProfileIdentityRecord,
    CommentProfileMetadataRecord,
    CommentReceiptRecord,
    CommentStepRecord,
    CommentTemplateRecord,
    CommentTemplateRevision,
)

if TYPE_CHECKING:
    from .schemas import CampaignCreate, TemplateCreate, TemplateUpdate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    return json.loads(value) if value else default


def _payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    raise TypeError("payload must be a Pydantic model or dictionary")


class CampaignStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.engine = create_campaign_engine(database_url)
        self.session_factory = create_campaign_session_factory(self.engine)

    def initialize(self) -> None:
        if self.database_url.startswith("sqlite:///"):
            database_path = self.database_url.removeprefix("sqlite:///")
            if database_path not in {":memory:", ""}:
                Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(self.engine)
        if self.database_url.startswith("sqlite"):
            with self.engine.begin() as connection:
                columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(comment_campaigns)")}
                if "prepare_generation" not in columns:
                    connection.exec_driver_sql("ALTER TABLE comment_campaigns ADD COLUMN prepare_generation INTEGER NOT NULL DEFAULT 0")
                if "reconcile_generation" not in columns:
                    connection.exec_driver_sql("ALTER TABLE comment_campaigns ADD COLUMN reconcile_generation INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        """Release SQLAlchemy's SQLite connections owned by this store."""

        self.engine.dispose()

    def create_template(self, payload: "TemplateCreate", template_id: str) -> dict:
        data = _payload(payload)
        now = _now()
        snapshot = self._template_snapshot(template_id, 1, data, enabled=True)
        with self.session_factory.begin() as session:
            session.add(CommentTemplateRecord(
                id=str(template_id), name=data["name"], description=data.get("description", ""),
                supported_modes_json=_json(data["supported_modes"]), language=data.get("language", ""),
                tags_json=_json(data.get("tags", [])), enabled=True, revision=1,
                created_at=now, updated_at=now,
            ))
            self._add_template_revision(session, template_id, 1, snapshot, now)
            return self._template_return(snapshot, enabled=True, created_at=now, updated_at=now)

    def update_template(self, template_id: str, expected_revision: int, payload: "TemplateUpdate") -> dict:
        data = _payload(payload)
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            record = session.get(CommentTemplateRecord, str(template_id))
            if record is None:
                raise CampaignNotFoundError(str(template_id))
            revision = expected + 1
            values = {
                "name": data["name"], "description": data.get("description", ""),
                "supported_modes_json": _json(data["supported_modes"]),
                "language": data.get("language", ""), "tags_json": _json(data.get("tags", [])),
                "revision": revision, "updated_at": now,
            }
            result = session.execute(update(CommentTemplateRecord).where(
                CommentTemplateRecord.id == str(template_id),
                CommentTemplateRecord.revision == expected,
            ).values(**values))
            if result.rowcount != 1:
                raise RevisionConflictError(str(template_id))
            snapshot = self._template_snapshot(template_id, revision, data, enabled=bool(record.enabled))
            self._add_template_revision(session, template_id, revision, snapshot, now)
            return self._template_return(snapshot, enabled=bool(record.enabled), created_at=record.created_at, updated_at=now)

    def disable_template(self, template_id: str, expected_revision: int) -> dict:
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            record = session.get(CommentTemplateRecord, str(template_id))
            if record is None:
                raise CampaignNotFoundError(str(template_id))
            previous = session.scalar(select(CommentTemplateRevision).where(
                CommentTemplateRevision.template_id == str(template_id),
                CommentTemplateRevision.revision == expected,
            ))
            if previous is None:
                raise RevisionConflictError(str(template_id))
            current = _loads(previous.snapshot_json, {})
            revision = expected + 1
            result = session.execute(update(CommentTemplateRecord).where(
                CommentTemplateRecord.id == str(template_id),
                CommentTemplateRecord.revision == expected,
            ).values(enabled=False, revision=revision, updated_at=now))
            if result.rowcount != 1:
                raise RevisionConflictError(str(template_id))
            snapshot = self._template_snapshot(template_id, revision, current, enabled=False)
            self._add_template_revision(session, template_id, revision, snapshot, now)
            return self._template_return(snapshot, enabled=False, created_at=record.created_at, updated_at=now)

    def list_templates(self) -> list[dict]:
        with self.session_factory.begin() as session:
            records = session.scalars(select(CommentTemplateRecord).order_by(CommentTemplateRecord.created_at, CommentTemplateRecord.id)).all()
            return [self._template_record(record) for record in records]

    def get_template(self, template_id: str, revision: int | None = None) -> dict | None:
        with self.session_factory.begin() as session:
            if revision is None:
                record = session.get(CommentTemplateRecord, str(template_id))
                if record is None:
                    return None
                snapshot = session.scalar(select(CommentTemplateRevision).where(
                    CommentTemplateRevision.template_id == str(template_id),
                    CommentTemplateRevision.revision == record.revision,
                ))
                result = self._template_record(record)
                result["steps"] = _loads(snapshot.snapshot_json, {}).get("steps", []) if snapshot else []
                return result
            record = session.scalar(select(CommentTemplateRevision).where(
                CommentTemplateRevision.template_id == str(template_id),
                CommentTemplateRevision.revision == int(revision),
            ))
            return _loads(record.snapshot_json, {}) if record is not None else None

    def upsert_profile_metadata(self, **fields: Any) -> dict:
        profile_ref = str(fields["profile_ref"])
        now = _now()
        values = self._metadata_values(fields)
        with self.session_factory.begin() as session:
            record = session.get(CommentProfileMetadataRecord, profile_ref)
            if record is None:
                if session.scalar(select(CommentProfileIdentityRecord.id).where(CommentProfileIdentityRecord.profile_ref == profile_ref)) is None:
                    raise CampaignNotFoundError(profile_ref)
                record = CommentProfileMetadataRecord(profile_ref=profile_ref, created_at=now, updated_at=now, **values)
                session.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
                record.updated_at = now
            session.flush()
            return self._metadata_record(record)

    def list_profile_metadata(self) -> list[dict]:
        with self.session_factory.begin() as session:
            rows = session.scalars(select(CommentProfileMetadataRecord).order_by(CommentProfileMetadataRecord.profile_ref)).all()
            return [self._metadata_record(row) for row in rows]

    def list_comment_profiles(self) -> list[dict]:
        """Return every safe identity with its optional campaign metadata."""

        with self.session_factory.begin() as session:
            identities = session.scalars(
                select(CommentProfileIdentityRecord).order_by(
                    CommentProfileIdentityRecord.profile_ref
                )
            ).all()
            metadata = {
                row.profile_ref: self._metadata_record(row)
                for row in session.scalars(select(CommentProfileMetadataRecord)).all()
            }
            rows: list[dict] = []
            for identity in identities:
                result = self._identity_public_record(identity)
                configured = metadata.get(identity.profile_ref)
                if configured is None:
                    result.update(
                        {
                            "configured": False,
                            "expected_username": "",
                            "enabled": False,
                            "login_verified": False,
                            "tags": [],
                            "language": "",
                            "region": "",
                            "cooldown_until": None,
                            "health_status": "unknown",
                        }
                    )
                else:
                    result.update(configured)
                    result["configured"] = True
                rows.append(result)
            return rows

    def get_profile_metadata(self, profile_ref: str) -> dict | None:
        with self.session_factory.begin() as session:
            row = session.get(CommentProfileMetadataRecord, str(profile_ref))
            return self._metadata_record(row) if row is not None else None

    def sync_profile_identities(self, raw_profiles: list[dict]) -> list[dict]:
        now = _now()
        profiles = [self._strict_raw_profile(profile) for profile in raw_profiles]
        profile_refs: list[str] = []
        with self.session_factory.begin() as session:
            for raw_id, name, status in profiles:
                record = session.scalar(select(CommentProfileIdentityRecord).where(
                    CommentProfileIdentityRecord.raw_adspower_id == raw_id
                ))
                if record is None:
                    try:
                        with session.begin_nested():
                            record = CommentProfileIdentityRecord(
                                profile_ref=f"profile_ref_{uuid4().hex}", raw_adspower_id=raw_id,
                                display_profile="", name="", status="", created_at=now, updated_at=now,
                            )
                            session.add(record)
                            session.flush()
                    except IntegrityError:
                        record = session.scalar(select(CommentProfileIdentityRecord).where(
                            CommentProfileIdentityRecord.raw_adspower_id == raw_id
                        ))
                        if record is None:
                            raise RuntimeError("profile identity synchronization failed")
                record.name = name
                record.status = status
                record.display_profile = self._display_profile(record.profile_ref, name)
                record.updated_at = now
                profile_refs.append(record.profile_ref)
            # Rebuild the public records from persisted fields. The raw ID is
            # only used above for local matching and never returned.
            rows = session.scalars(select(CommentProfileIdentityRecord).where(
                CommentProfileIdentityRecord.profile_ref.in_(profile_refs)
            )).all()
            public_rows = {row.profile_ref: self._identity_public_record(row) for row in rows}
        if len(public_rows) != len(set(profile_refs)):
            raise RuntimeError("profile identity synchronization failed")
        return [public_rows[profile_ref] for profile_ref in profile_refs]

    def get_raw_profile_id(self, profile_ref: str) -> str | None:
        """Internal gateway lookup. Never return this through a service or blueprint."""
        with self.session_factory.begin() as session:
            row = session.scalar(select(CommentProfileIdentityRecord.raw_adspower_id).where(CommentProfileIdentityRecord.profile_ref == str(profile_ref)))
            return row

    def get_profile_identity(self, profile_ref: str) -> dict | None:
        """Return the redacted identity record used by planning displays."""
        with self.session_factory.begin() as session:
            row = session.scalar(select(CommentProfileIdentityRecord).where(
                CommentProfileIdentityRecord.profile_ref == str(profile_ref)
            ))
            return self._identity_public_record(row) if row is not None else None

    def create_campaign(self, payload: "CampaignCreate", campaign_id: str, video_id: str, canonical_url: str) -> dict:
        data = _payload(payload)
        now = _now()
        requested_revision = data.get("template_revision")
        with self.session_factory.begin() as session:
            template = session.get(CommentTemplateRecord, str(data["template_id"]))
            if template is None:
                raise CampaignNotFoundError(str(data["template_id"]))
            template_revision = int(requested_revision or template.revision)
            exists = session.scalar(select(CommentTemplateRevision.id).where(
                CommentTemplateRevision.template_id == template.id,
                CommentTemplateRevision.revision == template_revision,
            ))
            if exists is None:
                raise CampaignNotFoundError(f"{template.id}@{template_revision}")
            record = CommentCampaignRecord(
                id=str(campaign_id), name=data["name"], mode=data["mode"],
                target_source=data["target_source"], target_reference=data["target_reference"],
                video_id=str(video_id), canonical_url=str(canonical_url), template_id=template.id,
                template_revision=template_revision, profile_refs_json=_json(data["profile_refs"]),
                batch_size=int(data.get("batch_size", 3)), allocation_seed=data.get("allocation_seed", ""),
                start_mode=data.get("start_mode", "manual"), scheduled_at=data.get("scheduled_at", ""),
                status=CampaignStatus.DRAFT.value, revision=1, created_at=now, updated_at=now,
            )
            session.add(record)
            session.flush()
            return self._campaign_record(record)

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self.session_factory.begin() as session:
            row = session.get(CommentCampaignRecord, str(campaign_id))
            return self._campaign_record(row) if row is not None else None

    def next_prepare_generation(self, campaign_id: str) -> int:
        """Allocate the next durable RQ generation before enqueueing it."""
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            if campaign is None:
                raise CampaignNotFoundError(str(campaign_id))
            generation = int(campaign.prepare_generation) + 1
            result = session.execute(update(CommentCampaignRecord).where(
                CommentCampaignRecord.id == campaign.id,
                CommentCampaignRecord.prepare_generation == campaign.prepare_generation,
            ).values(prepare_generation=generation, updated_at=_now()))
            if result.rowcount != 1:
                raise RevisionConflictError(campaign.id)
            return generation

    def pending_reconcile_prepare_generation(self, campaign_id: str) -> int | None:
        """Return the one unmarked generation that recovery may enqueue."""
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            if campaign is None:
                raise CampaignNotFoundError(str(campaign_id))
            if campaign.status not in {CampaignStatus.QUEUED.value, CampaignStatus.RUNNING.value}:
                return None
            generation = int(campaign.prepare_generation)
            if generation < 1 or int(campaign.reconcile_generation) >= generation:
                return None
            return generation

    def mark_reconcile_prepare_generation(self, campaign_id: str, generation: int) -> bool:
        """Mark only after RQ has accepted that fixed, idempotent job ID."""
        if type(generation) is not int or generation < 1:
            raise ValueError("prepare generation must be positive")
        with self.session_factory.begin() as session:
            result = session.execute(update(CommentCampaignRecord).where(
                CommentCampaignRecord.id == str(campaign_id),
                CommentCampaignRecord.prepare_generation >= generation,
                CommentCampaignRecord.reconcile_generation < generation,
            ).values(reconcile_generation=generation, updated_at=_now()))
            return result.rowcount == 1

    @staticmethod
    def pause_descendant_ids(assignments: list[dict], parent_assignment_id: str) -> list[str]:
        """Pure adjacency traversal used by the transactional branch pause below."""
        children: dict[str, list[str]] = {}
        for row in assignments:
            parent, child = row.get("parent_assignment_id"), row.get("assignment_id")
            if isinstance(parent, str) and isinstance(child, str):
                children.setdefault(parent, []).append(child)
        paused, pending, seen = [], list(children.get(parent_assignment_id, [])), {parent_assignment_id}
        while pending:
            child = pending.pop(0)
            if child in seen:
                continue
            seen.add(child)
            paused.append(child)
            pending.extend(children.get(child, []))
        return paused

    def pause_descendants(self, campaign_id: str, parent_assignment_id: str, error_code: str) -> list[str]:
        with self.session_factory.begin() as session:
            return self._pause_descendants_in_session(
                session, str(campaign_id), str(parent_assignment_id), str(error_code), _now()
            )

    def fail_assignment_and_pause_descendants(
        self, assignment_id: str, expected_revision: int, status: str, error_code: str
    ) -> dict:
        """Persist a terminal preparation failure and its branch pause atomically."""
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None or assignment.revision != expected:
                raise RevisionConflictError(str(assignment_id))
            target = transition_assignment(assignment.status, str(status))
            changed = session.execute(update(CommentAssignmentRecord).where(
                CommentAssignmentRecord.id == assignment.id,
                CommentAssignmentRecord.revision == expected,
            ).values(status=target, error_code=str(error_code), revision=expected + 1, updated_at=now))
            if changed.rowcount != 1:
                raise RevisionConflictError(assignment.id)
            self._pause_descendants_in_session(
                session, assignment.campaign_id, assignment.id, str(error_code), now
            )
            session.expire(assignment)
            return self._assignment_record(assignment)

    def verified_parent_receipt(self, campaign_id: str, assignment_id: str) -> dict | None:
        """Return parent receipt only if Assignment and durable Receipt are verified."""
        with self.session_factory.begin() as session:
            child = session.get(CommentAssignmentRecord, str(assignment_id))
            if child is None or child.campaign_id != str(campaign_id) or not child.parent_assignment_id:
                return None
            parent = session.get(CommentAssignmentRecord, child.parent_assignment_id)
            if parent is None or parent.campaign_id != child.campaign_id or parent.status != AssignmentStatus.PUBLISHED_VERIFIED.value:
                return None
            receipt = session.scalar(select(CommentReceiptRecord).where(
                CommentReceiptRecord.assignment_id == parent.id,
                CommentReceiptRecord.campaign_id == parent.campaign_id,
                CommentReceiptRecord.status == AssignmentStatus.PUBLISHED_VERIFIED.value,
            ))
            return self._receipt_record(receipt) if receipt is not None else None

    def eligible_assignment_ids(self, campaign_id: str) -> list[str]:
        """One transaction: roots plus children whose parent+receipt are verified."""
        with self.session_factory.begin() as session:
            rows = session.scalars(select(CommentAssignmentRecord).where(
                CommentAssignmentRecord.campaign_id == str(campaign_id),
                CommentAssignmentRecord.status.in_((AssignmentStatus.PLANNED.value, AssignmentStatus.WAITING_DEPENDENCY.value)),
            ).order_by(CommentAssignmentRecord.position, CommentAssignmentRecord.id)).all()
            result: list[str] = []
            for row in rows:
                if row.parent_assignment_id is None:
                    result.append(row.id)
                    continue
                parent = session.get(CommentAssignmentRecord, row.parent_assignment_id)
                if parent is None or parent.campaign_id != row.campaign_id or parent.status != AssignmentStatus.PUBLISHED_VERIFIED.value:
                    continue
                receipt = session.scalar(select(CommentReceiptRecord.id).where(CommentReceiptRecord.assignment_id == parent.id, CommentReceiptRecord.status == AssignmentStatus.PUBLISHED_VERIFIED.value))
                if receipt is not None:
                    result.append(row.id)
            return result

    def resume_verified_children(self, campaign_id: str, parent_assignment_id: str) -> list[str]:
        """Only direct children resume; deeper branches wait for their own parent."""
        with self.session_factory.begin() as session:
            parent = session.get(CommentAssignmentRecord, str(parent_assignment_id))
            if parent is None or parent.campaign_id != str(campaign_id) or parent.status != AssignmentStatus.PUBLISHED_VERIFIED.value:
                return []
            receipt = session.scalar(select(CommentReceiptRecord.id).where(CommentReceiptRecord.assignment_id == parent.id, CommentReceiptRecord.status == AssignmentStatus.PUBLISHED_VERIFIED.value))
            if receipt is None:
                return []
            rows = session.scalars(select(CommentAssignmentRecord).where(CommentAssignmentRecord.campaign_id == parent.campaign_id, CommentAssignmentRecord.parent_assignment_id == parent.id, CommentAssignmentRecord.status == AssignmentStatus.PAUSED_DEPENDENCY.value)).all()
            changed = []
            for row in rows:
                result = session.execute(update(CommentAssignmentRecord).where(CommentAssignmentRecord.id == row.id, CommentAssignmentRecord.revision == row.revision).values(status=AssignmentStatus.WAITING_DEPENDENCY.value, revision=row.revision + 1, updated_at=_now(), error_code=""))
                if result.rowcount != 1:
                    raise RevisionConflictError(row.id)
                changed.append(row.id)
            return changed

    def list_campaigns(self, status: str | None, limit: int, offset: int) -> list[dict]:
        self._page(limit, offset)
        with self.session_factory.begin() as session:
            statement = select(CommentCampaignRecord)
            if status is not None:
                statement = statement.where(CommentCampaignRecord.status == str(status))
            rows = session.scalars(statement.order_by(CommentCampaignRecord.created_at.desc(), CommentCampaignRecord.id.desc()).limit(limit).offset(offset)).all()
            if not rows:
                return []
            summaries = session.execute(
                select(
                    CommentAssignmentRecord.campaign_id,
                    func.count(CommentAssignmentRecord.id),
                    func.coalesce(func.sum(case((CommentAssignmentRecord.status == AssignmentStatus.AWAITING_STEP_APPROVAL.value, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((CommentAssignmentRecord.status.in_((AssignmentStatus.FAILED.value, AssignmentStatus.PAUSED.value, AssignmentStatus.PAUSED_DEPENDENCY.value, AssignmentStatus.PUBLISHED_UNVERIFIED.value)), 1), else_=0)), 0),
                ).where(CommentAssignmentRecord.campaign_id.in_([row.id for row in rows])).group_by(CommentAssignmentRecord.campaign_id)
            ).all()
            summary_by_campaign = {
                campaign_id: {"assignment_count": int(count), "awaiting_approval_count": int(awaiting), "abnormal_assignment_count": int(abnormal)}
                for campaign_id, count, awaiting, abnormal in summaries
            }
            return [{**self._campaign_record(row), **summary_by_campaign.get(row.id, {"assignment_count": 0, "awaiting_approval_count": 0, "abnormal_assignment_count": 0})} for row in rows]

    def replace_assignments(self, campaign_id: str, assignments: list[dict]) -> list[dict]:
        now = _now()
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            if campaign is None:
                raise CampaignNotFoundError(str(campaign_id))
            session.execute(delete(CommentAssignmentRecord).where(CommentAssignmentRecord.campaign_id == str(campaign_id)))
            records = []
            for position, assignment in enumerate(assignments, start=1):
                record = CommentAssignmentRecord(
                    id=str(assignment["assignment_id"]), campaign_id=str(campaign_id), step_id=str(assignment["step_id"]),
                    profile_ref=str(assignment["profile_ref"]), display_profile=str(assignment.get("display_profile", "")),
                    expected_username=str(assignment.get("expected_username", "")), role=str(assignment["role"]),
                    resolved_text=str(assignment.get("resolved_text", "")), parent_assignment_id=assignment.get("parent_assignment_id"),
                    position=int(assignment.get("position", position)), status=str(assignment.get("status", AssignmentStatus.PLANNED.value)),
                    revision=int(assignment.get("revision", 1)), locked_at=str(assignment.get("locked_at", "")),
                    error_code=str(assignment.get("error_code", "")), error_summary=str(assignment.get("error_summary", "")),
                    evidence_json=_json(assignment.get("evidence", {})), created_at=now, updated_at=now,
                )
                session.add(record)
                records.append(record)
            campaign.updated_at = now
            session.flush()
            return [self._assignment_record(record) for record in sorted(records, key=lambda value: (value.position, value.id))]

    def override_assignment_profile(
        self,
        campaign_id: str,
        assignment_id: str,
        expected_revision: int,
        profile_ref: str,
    ) -> dict:
        """CAS-change one pre-lock plan assignment without weakening eligibility."""

        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            if campaign is None:
                raise CampaignNotFoundError(str(campaign_id))
            if (
                campaign.status != CampaignStatus.PLANNED.value
                or campaign.locked_at
            ):
                raise RevisionConflictError(str(campaign_id))
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None or assignment.campaign_id != str(campaign_id):
                raise CampaignNotFoundError(str(assignment_id))
            if (
                assignment.revision != expected
                or assignment.status != AssignmentStatus.PLANNED.value
                or assignment.locked_at
            ):
                raise RevisionConflictError(str(assignment_id))
            if str(profile_ref) not in _loads(campaign.profile_refs_json, []):
                raise CampaignValidationError("allocation_unsatisfied")
            identity = session.scalar(
                select(CommentProfileIdentityRecord).where(
                    CommentProfileIdentityRecord.profile_ref == str(profile_ref)
                )
            )
            metadata = session.get(CommentProfileMetadataRecord, str(profile_ref))
            if identity is None or metadata is None:
                raise CampaignValidationError("allocation_unsatisfied")
            duplicate = session.scalar(
                select(CommentAssignmentRecord.id).where(
                    CommentAssignmentRecord.campaign_id == str(campaign_id),
                    CommentAssignmentRecord.profile_ref == str(profile_ref),
                    CommentAssignmentRecord.id != str(assignment_id),
                )
            )
            if duplicate is not None:
                raise CampaignValidationError("allocation_unsatisfied")
            template = _loads(campaign.template_snapshot_json, {})
            steps = template.get("steps") if isinstance(template, dict) else None
            step = next(
                (
                    item for item in (steps or [])
                    if isinstance(item, dict) and item.get("id") == assignment.step_id
                ),
                None,
            )
            if step is None:
                raise CampaignValidationError("allocation_unsatisfied")
            profile = {
                **self._metadata_record(metadata),
                "display_profile": identity.display_profile,
            }
            if not profile_matches(step, profile):
                raise CampaignValidationError("allocation_unsatisfied")
            assignment_result = session.execute(
                update(CommentAssignmentRecord)
                .where(
                    CommentAssignmentRecord.id == str(assignment_id),
                    CommentAssignmentRecord.campaign_id == str(campaign_id),
                    CommentAssignmentRecord.revision == expected,
                    CommentAssignmentRecord.status == AssignmentStatus.PLANNED.value,
                    CommentAssignmentRecord.locked_at == "",
                )
                .values(
                    profile_ref=str(profile_ref),
                    display_profile=identity.display_profile,
                    expected_username=metadata.expected_username,
                    revision=expected + 1,
                    updated_at=now,
                )
            )
            if assignment_result.rowcount != 1:
                raise RevisionConflictError(str(assignment_id))
            campaign_result = session.execute(
                update(CommentCampaignRecord)
                .where(
                    CommentCampaignRecord.id == str(campaign_id),
                    CommentCampaignRecord.revision == campaign.revision,
                    CommentCampaignRecord.status == CampaignStatus.PLANNED.value,
                    CommentCampaignRecord.locked_at == "",
                )
                .values(revision=campaign.revision + 1, updated_at=now)
            )
            if campaign_result.rowcount != 1:
                raise RevisionConflictError(str(campaign_id))
            session.expire(assignment)
            session.expire(campaign)
            return {
                "campaign": self._campaign_record(campaign),
                "assignment": self._assignment_record(assignment),
            }

    def replace_campaign_plan(
        self,
        campaign_id: str,
        expected_revision: int,
        assignments: list[dict],
        *,
        allocation_seed: str,
        template_snapshot: dict,
        profile_snapshot: list[dict],
        content_snapshot: Any,
    ) -> dict:
        """CAS-replace a whole pre-lock plan in one transaction.

        The snapshots are stored with the plan so a later lock cannot resolve a
        library item again and silently change the text a reviewer saw.
        """
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            if campaign is None:
                raise CampaignNotFoundError(str(campaign_id))
            if campaign.revision != expected:
                raise RevisionConflictError(str(campaign_id))
            if campaign.status not in {CampaignStatus.DRAFT.value, CampaignStatus.PLANNED.value}:
                raise RevisionConflictError(str(campaign_id))
            session.execute(delete(CommentAssignmentRecord).where(CommentAssignmentRecord.campaign_id == str(campaign_id)))
            records = self._insert_assignments(session, str(campaign_id), assignments, now)
            result = session.execute(update(CommentCampaignRecord).where(
                CommentCampaignRecord.id == str(campaign_id),
                CommentCampaignRecord.revision == expected,
            ).values(
                status=CampaignStatus.PLANNED.value, allocation_seed=str(allocation_seed),
                template_snapshot_json=_json(template_snapshot), profile_snapshot_json=_json(profile_snapshot),
                content_snapshot_json=_json(content_snapshot), revision=expected + 1, updated_at=now,
            ))
            if result.rowcount != 1:
                raise RevisionConflictError(str(campaign_id))
            session.expire(campaign)
            return {
                "campaign": self._campaign_record(campaign),
                "assignments": [self._assignment_record(record) for record in sorted(records, key=lambda value: (value.position, value.id))],
            }

    def lock_campaign_plan(self, campaign_id: str, expected_revision: int) -> dict:
        """Atomically seal a planned assignment set for campaign-level review."""
        from .domain import transition_campaign

        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            if campaign is None:
                raise CampaignNotFoundError(str(campaign_id))
            if campaign.revision != expected:
                raise RevisionConflictError(str(campaign_id))
            assignments = session.scalars(select(CommentAssignmentRecord).where(
                CommentAssignmentRecord.campaign_id == str(campaign_id)
            ).order_by(CommentAssignmentRecord.position, CommentAssignmentRecord.id)).all()
            if not assignments:
                raise CampaignValidationError("allocation_unsatisfied")
            if any(row.status != AssignmentStatus.PLANNED.value or row.locked_at for row in assignments):
                raise RevisionConflictError(str(campaign_id))
            self._validate_locked_plan_shape(campaign, assignments)
            self._validate_locked_profiles(session, campaign, assignments)
            target = transition_campaign(campaign.status, CampaignStatus.AWAITING_CAMPAIGN_APPROVAL)
            result = session.execute(update(CommentCampaignRecord).where(
                CommentCampaignRecord.id == str(campaign_id),
                CommentCampaignRecord.revision == expected,
            ).values(status=target, locked_at=now, revision=expected + 1, updated_at=now))
            if result.rowcount != 1:
                raise RevisionConflictError(str(campaign_id))
            for assignment in assignments:
                assignment_result = session.execute(update(CommentAssignmentRecord).where(
                    CommentAssignmentRecord.id == assignment.id,
                    CommentAssignmentRecord.campaign_id == str(campaign_id),
                    CommentAssignmentRecord.revision == assignment.revision,
                    CommentAssignmentRecord.status == AssignmentStatus.PLANNED.value,
                    CommentAssignmentRecord.locked_at == "",
                ).values(locked_at=now, revision=assignment.revision + 1, updated_at=now))
                if assignment_result.rowcount != 1:
                    raise RevisionConflictError(assignment.id)
            session.expire(campaign)
            return self._campaign_record(campaign)

    @staticmethod
    def _validate_locked_plan_shape(campaign: CommentCampaignRecord, assignments: list[CommentAssignmentRecord]) -> None:
        template = _loads(campaign.template_snapshot_json, {})
        content = _loads(campaign.content_snapshot_json, {})
        profiles = _loads(campaign.profile_snapshot_json, [])
        if not isinstance(template, dict) or not isinstance(content, dict) or not isinstance(profiles, list):
            raise CampaignValidationError("allocation_unsatisfied")
        steps = template.get("steps")
        content_steps = content.get("steps")
        libraries = content.get("libraries")
        if (
            not isinstance(steps, list)
            or not isinstance(content_steps, list)
            or not isinstance(libraries, list)
            or any(not isinstance(step, dict) for step in steps)
            or any(not isinstance(step, dict) for step in content_steps)
            or any(not isinstance(library, dict) for library in libraries)
        ):
            raise CampaignValidationError("allocation_unsatisfied")
        step_ids = [str(step.get("id") or "") for step in steps]
        content_by_step = {str(step.get("step_id") or ""): step for step in content_steps}
        frozen_profiles = {str(profile.get("profile_ref") or "") for profile in profiles if isinstance(profile, dict)}
        assignment_step_ids = [assignment.step_id for assignment in assignments]
        assignment_profiles = [assignment.profile_ref for assignment in assignments]
        assignments_by_step = {assignment.step_id: assignment for assignment in assignments}
        libraries_by_id = {str(library.get("content_library_id") or ""): library for library in libraries}
        if (
            not step_ids
            or len(step_ids) != len(set(step_ids))
            or len(assignments) != len(step_ids)
            or set(assignment_step_ids) != set(step_ids)
            or len(content_steps) != len(step_ids)
            or set(content_by_step) != set(step_ids)
            or len(libraries_by_id) != len(libraries)
            or len(frozen_profiles) != len(profiles)
            or any(profile not in frozen_profiles for profile in assignment_profiles)
            or len(assignment_profiles) != len(set(assignment_profiles))
            or any(content_by_step[assignment.step_id].get("resolved_text") != assignment.resolved_text for assignment in assignments)
        ):
            raise CampaignValidationError("allocation_unsatisfied")
        parents = {str(step["id"]): step.get("parent_step_id") or None for step in steps}
        if any(parent is not None and parent not in parents for parent in parents.values()):
            raise CampaignValidationError("allocation_unsatisfied")
        expected_positions: dict[str, int] = {}
        original_index = {str(step["id"]): index for index, step in enumerate(steps)}
        pending = set(parents)
        while pending:
            ready = sorted((step_id for step_id in pending if parents[step_id] not in pending), key=original_index.__getitem__)
            if not ready:
                raise CampaignValidationError("allocation_unsatisfied")
            for step_id in ready:
                expected_positions[step_id] = len(expected_positions) + 1
                pending.remove(step_id)
        for step_id, step in ((str(step["id"]), step) for step in steps):
            assignment = assignments_by_step[step_id]
            parent_step_id = parents[step_id]
            expected_role = "commenter" if campaign.mode == "independent" else ("owner" if parent_step_id is None else "participant")
            expected_parent_assignment_id = assignments_by_step[parent_step_id].id if parent_step_id is not None else None
            content_step = content_by_step[step_id]
            if (
                assignment.role != expected_role
                or assignment.parent_assignment_id != expected_parent_assignment_id
                or assignment.position != expected_positions[step_id]
                or content_step.get("content_source") != step.get("content_source")
                or content_step.get("resolved_text") != assignment.resolved_text
            ):
                raise CampaignValidationError("allocation_unsatisfied")
            if step.get("content_source") == "fixed":
                if (
                    content_step.get("content_library_id") != ""
                    or content_step.get("content_item_id") != ""
                    or content_step.get("resolved_text") != step.get("fixed_text")
                ):
                    raise CampaignValidationError("allocation_unsatisfied")
                continue
            library_id = str(step.get("content_library_id") or "")
            library = libraries_by_id.get(library_id)
            items = library.get("items") if library is not None else None
            if (
                not library_id
                or content_step.get("content_library_id") != library_id
                or (
                    bool(step.get("content_item_id"))
                    and content_step.get("content_item_id") != step.get("content_item_id")
                )
                or not isinstance(items, list)
                or not any(
                    isinstance(item, dict)
                    and item.get("content_item_id") == content_step.get("content_item_id")
                    and item.get("text") == content_step.get("resolved_text")
                    for item in items
                )
            ):
                raise CampaignValidationError("allocation_unsatisfied")

    @staticmethod
    def _validate_locked_profiles(session, campaign: CommentCampaignRecord, assignments: list[CommentAssignmentRecord]) -> None:
        now = datetime.now(timezone.utc)
        steps = {str(step.get("id") or ""): step for step in _loads(campaign.template_snapshot_json, {}).get("steps", [])}
        for assignment in assignments:
            metadata = session.get(CommentProfileMetadataRecord, assignment.profile_ref)
            step = steps.get(assignment.step_id)
            tags = set(_loads(metadata.tags_json, [])) if metadata is not None else set()
            required = set(step.get("required_profile_tags", [])) if step else set()
            excluded = set(step.get("excluded_profile_tags", [])) if step else set()
            language = str(step.get("language") or "") if step else ""
            if (
                metadata is None
                or step is None
                or not metadata.enabled
                or not metadata.login_verified
                or metadata.health_status != "healthy"
                or not metadata.expected_username
                or metadata.expected_username != assignment.expected_username
                or not required <= tags
                or bool(excluded & tags)
                or bool(language and metadata.language != language)
            ):
                raise CampaignValidationError("allocation_unsatisfied")
            if metadata.cooldown_until:
                try:
                    cooldown = datetime.fromisoformat(metadata.cooldown_until.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise CampaignValidationError("allocation_unsatisfied") from exc
                if cooldown.tzinfo is None or cooldown > now:
                    raise CampaignValidationError("allocation_unsatisfied")

    def list_assignments(self, campaign_id: str) -> list[dict]:
        with self.session_factory.begin() as session:
            rows = session.scalars(select(CommentAssignmentRecord).where(CommentAssignmentRecord.campaign_id == str(campaign_id)).order_by(CommentAssignmentRecord.position, CommentAssignmentRecord.id)).all()
            return [self._assignment_record(row) for row in rows]

    def get_assignment(self, assignment_id: str) -> dict | None:
        with self.session_factory.begin() as session:
            row = session.get(CommentAssignmentRecord, str(assignment_id))
            return self._assignment_record(row) if row is not None else None

    @staticmethod
    def _insert_assignments(session, campaign_id: str, assignments: list[dict], now: str) -> list[CommentAssignmentRecord]:
        records = []
        for position, assignment in enumerate(assignments, start=1):
            record = CommentAssignmentRecord(
                id=str(assignment["assignment_id"]), campaign_id=campaign_id, step_id=str(assignment["step_id"]),
                profile_ref=str(assignment["profile_ref"]), display_profile=str(assignment.get("display_profile", "")),
                expected_username=str(assignment.get("expected_username", "")), role=str(assignment["role"]),
                resolved_text=str(assignment.get("resolved_text", "")), parent_assignment_id=assignment.get("parent_assignment_id"),
                position=int(assignment.get("position", position)), status=str(assignment.get("status", AssignmentStatus.PLANNED.value)),
                revision=int(assignment.get("revision", 1)), locked_at=str(assignment.get("locked_at", "")),
                error_code=str(assignment.get("error_code", "")), error_summary=str(assignment.get("error_summary", "")),
                evidence_json=_json(assignment.get("evidence", {})), created_at=now, updated_at=now,
            )
            session.add(record)
            records.append(record)
        session.flush()
        return records

    def update_assignment_status(self, assignment_id: str, expected_revision: int, status: str, **fields: Any) -> dict:
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            row = session.get(CommentAssignmentRecord, str(assignment_id))
            if row is None:
                raise CampaignNotFoundError(str(assignment_id))
            if row.revision != expected:
                raise RevisionConflictError(str(assignment_id))
            values: dict[str, Any] = {"status": transition_assignment(row.status, status), "revision": expected + 1, "updated_at": now}
            for field in ("error_code", "error_summary", "locked_at"):
                if field in fields:
                    values[field] = str(fields[field] or "")
            if "evidence" in fields:
                values["evidence_json"] = _json(fields["evidence"])
            result = session.execute(update(CommentAssignmentRecord).where(CommentAssignmentRecord.id == str(assignment_id), CommentAssignmentRecord.revision == expected).values(**values))
            if result.rowcount != 1:
                raise RevisionConflictError(str(assignment_id))
            session.expire(row)
            result = self._assignment_record(row)
        return result

    def reject_submit_and_pause_descendants(
        self, campaign_id: str, assignment_id: str, expected_revision: int, reason: str
    ) -> dict:
        """Reject exactly one approval revision and pause only its descendant branch."""
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None or assignment.campaign_id != str(campaign_id):
                raise CampaignNotFoundError(str(assignment_id))
            if assignment.revision != expected:
                raise RevisionConflictError(str(assignment_id))
            target = transition_assignment(
                assignment.status, AssignmentStatus.PAUSED.value
            )
            changed = session.execute(update(CommentAssignmentRecord).where(
                CommentAssignmentRecord.id == assignment.id,
                CommentAssignmentRecord.revision == expected,
            ).values(
                status=target, error_code="approval_rejected", error_summary=str(reason),
                revision=expected + 1, updated_at=now,
            ))
            if changed.rowcount != 1:
                raise RevisionConflictError(assignment.id)
            self._pause_descendants_in_session(
                session, assignment.campaign_id, assignment.id, "approval_rejected", now
            )
            session.expire(assignment)
            return self._assignment_record(assignment)

    def resolve_unverified_assignment(
        self, campaign_id: str, assignment_id: str, expected_revision: int, resolution: str, reason: str
    ) -> dict:
        """Resolve a durable uncertain receipt without ever creating a replay submit."""
        expected = self._expected_revision(expected_revision)
        if resolution not in {"published", "not_published"}:
            raise ValueError("resolution is invalid")
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None or assignment.campaign_id != str(campaign_id):
                raise CampaignNotFoundError(str(assignment_id))
            if assignment.revision != expected:
                raise RevisionConflictError(str(assignment_id))
            if resolution == "published":
                target = transition_assignment(
                    assignment.status, AssignmentStatus.PUBLISHED_VERIFIED.value
                )
                receipt_status = target
                error_code = ""
            else:
                paused = transition_assignment(
                    assignment.status, AssignmentStatus.PAUSED.value
                )
                target = transition_assignment(
                    paused, AssignmentStatus.WAITING_DEPENDENCY.value
                )
                receipt_status = AssignmentStatus.PUBLISHED_UNVERIFIED.value
                error_code = "comment_receipt_unverified"
            receipt = session.scalar(select(CommentReceiptRecord).where(CommentReceiptRecord.assignment_id == assignment.id))
            if receipt is None:
                raise CampaignValidationError("comment_receipt_unverified")
            changed = session.execute(update(CommentAssignmentRecord).where(
                CommentAssignmentRecord.id == assignment.id,
                CommentAssignmentRecord.revision == expected,
            ).values(
                status=target, error_code=error_code,
                error_summary=str(reason), revision=expected + 1, updated_at=now,
            ))
            if changed.rowcount != 1:
                raise RevisionConflictError(assignment.id)
            receipt.status, receipt.updated_at = receipt_status, now
            attempt_no = int(session.scalar(select(func.coalesce(func.max(CommentAttemptRecord.attempt_no), 0)).where(CommentAttemptRecord.assignment_id == assignment.id)) or 0) + 1
            session.add(CommentAttemptRecord(
                id=f"attempt_{uuid4().hex}", campaign_id=assignment.campaign_id,
                assignment_id=assignment.id, profile_ref=assignment.profile_ref,
                attempt_no=attempt_no, stage="operator_resolution", status="succeeded",
                error_code=error_code, error_summary=str(reason), evidence_paths_json="[]",
                started_at=now, finished_at=now,
            ))
            session.expire(assignment)
            return self._assignment_record(assignment)

    def approve_campaign_for_queue(self, campaign_id: str, expected_revision: int) -> dict:
        """Durably record the campaign's queue intent before RQ is contacted."""
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            row = session.get(CommentCampaignRecord, str(campaign_id))
            if row is None:
                raise CampaignNotFoundError(str(campaign_id))
            if (
                row.status == CampaignStatus.QUEUED.value
                and row.revision == expected + 1
            ):
                return self._campaign_record(row)
            if row.revision != expected:
                raise RevisionConflictError(str(campaign_id))
            target = transition_campaign(row.status, CampaignStatus.QUEUED.value)
            result = session.execute(update(CommentCampaignRecord).where(
                CommentCampaignRecord.id == str(campaign_id),
                CommentCampaignRecord.revision == expected,
            ).values(status=target, revision=expected + 1, updated_at=now))
            if result.rowcount != 1:
                raise RevisionConflictError(str(campaign_id))
            session.expire(row)
            return self._campaign_record(row)

    def create_submit_approval(
        self, campaign_id: str, assignment_id: str, revision: int, approval_token: str
    ) -> dict:
        """Persist a one-time approval before any submit work can be enqueued."""
        expected = self._expected_revision(revision)
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None or assignment.campaign_id != str(campaign_id):
                raise CampaignNotFoundError(str(assignment_id))
            if assignment.revision != expected:
                raise RevisionConflictError(str(assignment_id))
            if assignment.status != AssignmentStatus.AWAITING_STEP_APPROVAL.value:
                raise CampaignValidationError("approval_revision_mismatch")
            campaign = session.get(CommentCampaignRecord, assignment.campaign_id)
            if campaign is None or campaign.status != CampaignStatus.RUNNING.value:
                raise CampaignValidationError("approval_revision_mismatch")
            record = session.scalar(select(CommentApprovalRecord).where(
                CommentApprovalRecord.assignment_id == assignment.id,
                CommentApprovalRecord.revision == expected,
            ))
            if record is None:
                record = CommentApprovalRecord(
                    id=f"approval_{uuid4().hex}", campaign_id=assignment.campaign_id,
                    assignment_id=assignment.id, revision=expected,
                    approval_token=str(approval_token), approved_at=now, consumed_at="",
                )
                try:
                    with session.begin_nested():
                        session.add(record)
                        session.flush()
                except IntegrityError:
                    record = session.scalar(select(CommentApprovalRecord).where(
                        CommentApprovalRecord.assignment_id == assignment.id,
                        CommentApprovalRecord.revision == expected,
                    ))
                    if record is None:
                        raise
            if record.consumed_at:
                raise CampaignValidationError("approval_revision_mismatch")
            return self._approval_record(record)

    def get_approval(self, assignment_id: str, revision: int) -> dict | None:
        with self.session_factory.begin() as session:
            record = session.scalar(select(CommentApprovalRecord).where(
                CommentApprovalRecord.assignment_id == str(assignment_id),
                CommentApprovalRecord.revision == int(revision),
            ))
            return self._approval_record(record) if record is not None else None

    def consume_submit_approval(self, campaign_id: str, assignment_id: str, revision: int) -> dict:
        """Atomically consume exactly one approval for the current awaiting revision."""
        expected = self._expected_revision(revision)
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if (assignment is None or assignment.campaign_id != str(campaign_id)
                    or assignment.revision != expected
                    or assignment.status != AssignmentStatus.AWAITING_STEP_APPROVAL.value):
                raise CampaignValidationError("approval_revision_mismatch")
            campaign = session.get(CommentCampaignRecord, assignment.campaign_id)
            if campaign is None or campaign.status != CampaignStatus.RUNNING.value:
                raise CampaignValidationError("approval_revision_mismatch")
            result = session.execute(update(CommentApprovalRecord).where(
                CommentApprovalRecord.assignment_id == assignment.id,
                CommentApprovalRecord.revision == expected,
                CommentApprovalRecord.consumed_at == "",
            ).values(consumed_at=now))
            if result.rowcount != 1:
                raise CampaignValidationError("approval_revision_mismatch")
            return self._assignment_record(assignment)

    def invalidate_submit_approval(self, campaign_id: str, assignment_id: str, revision: int) -> dict:
        """Invalidate an approval after re-verification changes without clicking."""
        expected = self._expected_revision(revision)
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if (assignment is None or assignment.campaign_id != str(campaign_id)
                    or assignment.revision != expected
                    or assignment.status != AssignmentStatus.AWAITING_STEP_APPROVAL.value):
                raise CampaignValidationError("approval_revision_mismatch")
            session.execute(update(CommentApprovalRecord).where(
                CommentApprovalRecord.assignment_id == assignment.id,
                CommentApprovalRecord.revision == expected,
            ).values(consumed_at=now))
            result = session.execute(update(CommentAssignmentRecord).where(
                CommentAssignmentRecord.id == assignment.id,
                CommentAssignmentRecord.revision == expected,
            ).values(revision=expected + 1, updated_at=now))
            if result.rowcount != 1:
                raise RevisionConflictError(assignment.id)
            session.expire(assignment)
            return self._assignment_record(assignment)

    def begin_submitting(self, campaign_id: str, assignment_id: str, revision: int) -> dict:
        """Last pre-click CAS: Campaign must still be running and revision exact."""
        expected = self._expected_revision(revision)
        now = _now()
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if (campaign is None or campaign.status != CampaignStatus.RUNNING.value
                    or assignment is None or assignment.campaign_id != campaign.id
                    or assignment.revision != expected
                    or assignment.status != AssignmentStatus.AWAITING_STEP_APPROVAL.value):
                raise CampaignValidationError("approval_revision_mismatch")
            result = session.execute(update(CommentAssignmentRecord).where(
                CommentAssignmentRecord.id == assignment.id,
                CommentAssignmentRecord.revision == expected,
                CommentAssignmentRecord.status == AssignmentStatus.AWAITING_STEP_APPROVAL.value,
                exists(select(CommentCampaignRecord.id).where(CommentCampaignRecord.id == str(campaign_id), CommentCampaignRecord.status == CampaignStatus.RUNNING.value)),
            ).values(status=AssignmentStatus.SUBMITTING.value, revision=expected + 1, updated_at=now))
            if result.rowcount != 1:
                raise RevisionConflictError(assignment.id)
            session.expire(assignment)
            return self._assignment_record(assignment)

    def list_approvals(self, campaign_id: str) -> list[dict]:
        with self.session_factory.begin() as session:
            records = session.scalars(select(CommentApprovalRecord).where(
                CommentApprovalRecord.campaign_id == str(campaign_id)
            ).order_by(CommentApprovalRecord.approved_at, CommentApprovalRecord.id)).all()
            return [self._approval_record(record) for record in records]

    def transition_campaign_status(self, campaign_id: str, expected_revision: int, status: str, *, pause_reason: str = "") -> dict:
        """CAS campaign state update kept separate from the orchestration service."""
        from .domain import transition_campaign

        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            row = session.get(CommentCampaignRecord, str(campaign_id))
            if row is None:
                raise CampaignNotFoundError(str(campaign_id))
            if row.revision != expected:
                raise RevisionConflictError(str(campaign_id))
            target = transition_campaign(row.status, status)
            result = session.execute(update(CommentCampaignRecord).where(CommentCampaignRecord.id == str(campaign_id), CommentCampaignRecord.revision == expected).values(status=target, pause_reason=str(pause_reason or ""), revision=expected + 1, updated_at=now))
            if result.rowcount != 1:
                raise RevisionConflictError(str(campaign_id))
            session.expire(row)
            return self._campaign_record(row)

    def update_campaign_status(self, campaign_id: str, expected_revision: int, status: str, *, pause_reason: str = "") -> dict:
        """Backward-compatible spelling; orchestration uses transition_campaign_status."""
        return self.transition_campaign_status(campaign_id, expected_revision, status, pause_reason=pause_reason)

    def append_attempt(self, assignment_id: str, stage: str, status: str, **fields: Any) -> dict:
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None:
                raise CampaignNotFoundError(str(assignment_id))
            attempt_no = int(session.scalar(select(func.coalesce(func.max(CommentAttemptRecord.attempt_no), 0)).where(CommentAttemptRecord.assignment_id == assignment.id)) or 0) + 1
            record = CommentAttemptRecord(
                id=str(fields.get("attempt_id") or f"attempt_{uuid4().hex}"), campaign_id=assignment.campaign_id,
                assignment_id=assignment.id, profile_ref=assignment.profile_ref, attempt_no=attempt_no,
                stage=str(stage), status=str(status), error_code=str(fields.get("error_code") or ""),
                error_summary=str(fields.get("error_summary") or ""), evidence_paths_json=_json(fields.get("evidence_paths", [])),
                started_at=str(fields.get("started_at") or now), finished_at=str(fields.get("finished_at") or (now if status in {"succeeded", "failed", "cancelled"} else "")),
            )
            session.add(record)
            session.flush()
            return self._attempt_record(record)

    def save_receipt(self, assignment_id: str, receipt: dict) -> dict:
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None:
                raise CampaignNotFoundError(str(assignment_id))
            record = session.scalar(select(CommentReceiptRecord).where(CommentReceiptRecord.assignment_id == assignment.id))
            if record is None:
                record = CommentReceiptRecord(id=str(receipt.get("receipt_id") or f"receipt_{uuid4().hex}"), campaign_id=assignment.campaign_id, assignment_id=assignment.id, receipt_json=_json(receipt), status=str(receipt.get("status") or "pending"), created_at=now, updated_at=now)
                session.add(record)
            else:
                record.receipt_json = _json(receipt)
                record.status = str(receipt.get("status") or record.status)
                record.updated_at = now
            session.flush()
            return self._receipt_record(record)

    def save_receipt_and_transition(self, assignment_id: str, expected_revision: int, receipt: dict, status: str, *, error_code: str = "", pause_descendants_error_code: str = "") -> dict:
        """Persist receipt evidence and its terminal verification status together."""
        expected = self._expected_revision(expected_revision)
        now = _now()
        with self.session_factory.begin() as session:
            assignment = session.get(CommentAssignmentRecord, str(assignment_id))
            if assignment is None or assignment.revision != expected:
                raise RevisionConflictError(str(assignment_id))
            target = transition_assignment(assignment.status, status)
            record = session.scalar(select(CommentReceiptRecord).where(CommentReceiptRecord.assignment_id == assignment.id))
            if record is None:
                record = CommentReceiptRecord(id=str(receipt.get("receipt_id") or f"receipt_{uuid4().hex}"), campaign_id=assignment.campaign_id, assignment_id=assignment.id, receipt_json=_json(receipt), status=target, created_at=now, updated_at=now)
                session.add(record)
            else:
                record.receipt_json, record.status, record.updated_at = _json(receipt), target, now
            result = session.execute(update(CommentAssignmentRecord).where(CommentAssignmentRecord.id == assignment.id, CommentAssignmentRecord.revision == expected).values(status=target, revision=expected + 1, error_code=str(error_code), updated_at=now))
            if result.rowcount != 1:
                raise RevisionConflictError(assignment.id)
            if pause_descendants_error_code:
                self._pause_descendants_in_session(
                    session, assignment.campaign_id, assignment.id,
                    str(pause_descendants_error_code), now,
                )
            session.flush()
            session.expire(assignment)
            return self._assignment_record(assignment)

    def _pause_descendants_in_session(self, session, campaign_id: str, parent_assignment_id: str, error_code: str, now: str) -> list[str]:
        rows = session.scalars(select(CommentAssignmentRecord).where(
            CommentAssignmentRecord.campaign_id == str(campaign_id)
        )).all()
        if not any(row.id == str(parent_assignment_id) for row in rows):
            raise CampaignNotFoundError(str(parent_assignment_id))
        ids = self.pause_descendant_ids([
            {"assignment_id": row.id, "parent_assignment_id": row.parent_assignment_id}
            for row in rows
        ], str(parent_assignment_id))
        by_id = {row.id: row for row in rows}
        changed: list[str] = []
        for identifier in ids:
            row = by_id[identifier]
            if row.status in {AssignmentStatus.PLANNED.value, AssignmentStatus.WAITING_DEPENDENCY.value, AssignmentStatus.OPENING_PROFILE.value, AssignmentStatus.LOCATING_VIDEO.value, AssignmentStatus.LOCATING_PARENT.value, AssignmentStatus.PREPARING_COMMENT.value, AssignmentStatus.AWAITING_STEP_APPROVAL.value}:
                result = session.execute(update(CommentAssignmentRecord).where(
                    CommentAssignmentRecord.id == row.id,
                    CommentAssignmentRecord.revision == row.revision,
                ).values(status=AssignmentStatus.PAUSED_DEPENDENCY.value, error_code=str(error_code), revision=row.revision + 1, updated_at=now))
                if result.rowcount != 1:
                    raise RevisionConflictError(row.id)
                changed.append(identifier)
        return changed

    def list_receipts(self, campaign_id: str) -> list[dict]:
        with self.session_factory.begin() as session:
            rows = session.scalars(select(CommentReceiptRecord).where(CommentReceiptRecord.campaign_id == str(campaign_id)).order_by(CommentReceiptRecord.created_at, CommentReceiptRecord.id)).all()
            return [self._receipt_record(row) for row in rows]

    def list_attempts(self, campaign_id: str) -> list[dict]:
        with self.session_factory.begin() as session:
            rows = session.scalars(select(CommentAttemptRecord).where(CommentAttemptRecord.campaign_id == str(campaign_id)).order_by(CommentAttemptRecord.started_at, CommentAttemptRecord.attempt_no, CommentAttemptRecord.id)).all()
            return [self._attempt_record(row) for row in rows]

    def reconcile_campaign_state(self, campaign_id: str) -> dict[str, int]:
        """Persist one campaign's conservative restart recovery in one transaction.

        There is deliberately no Redis or queue operation here.  A recovery may
        create preparation work later, but must never infer that an uncertain
        browser click should be replayed.
        """
        now = _now()
        with self.session_factory.begin() as session:
            campaign = session.get(CommentCampaignRecord, str(campaign_id))
            if campaign is None:
                raise CampaignNotFoundError(str(campaign_id))
            rows = session.scalars(select(CommentAssignmentRecord).where(
                CommentAssignmentRecord.campaign_id == campaign.id
            ).order_by(CommentAssignmentRecord.position, CommentAssignmentRecord.id)).all()
            interrupted = approval_recovered = 0
            for assignment in rows:
                if assignment.status not in {AssignmentStatus.SUBMITTING.value, AssignmentStatus.VERIFYING_RECEIPT.value}:
                    continue
                receipt = session.scalar(select(CommentReceiptRecord).where(CommentReceiptRecord.assignment_id == assignment.id))
                payload = _loads(receipt.receipt_json, {}) if receipt is not None else {}
                payload.update({
                    "receipt_id": receipt.id if receipt is not None else f"receipt_{uuid4().hex}",
                    "status": AssignmentStatus.PUBLISHED_UNVERIFIED.value,
                    "video_id": campaign.video_id, "profile_ref": assignment.profile_ref,
                    "expected_username": assignment.expected_username,
                    "normalized_text": assignment.resolved_text,
                    "recovery_reason": "comment_submit_uncertain",
                })
                if receipt is None:
                    session.add(CommentReceiptRecord(
                        id=payload["receipt_id"], campaign_id=campaign.id, assignment_id=assignment.id,
                        receipt_json=_json(payload), status=AssignmentStatus.PUBLISHED_UNVERIFIED.value,
                        created_at=now, updated_at=now,
                    ))
                else:
                    receipt.receipt_json, receipt.status, receipt.updated_at = _json(payload), AssignmentStatus.PUBLISHED_UNVERIFIED.value, now
                revision = assignment.revision
                changed = session.execute(update(CommentAssignmentRecord).where(
                    CommentAssignmentRecord.id == assignment.id,
                    CommentAssignmentRecord.revision == revision,
                    CommentAssignmentRecord.status.in_((AssignmentStatus.SUBMITTING.value, AssignmentStatus.VERIFYING_RECEIPT.value)),
                ).values(status=AssignmentStatus.PUBLISHED_UNVERIFIED.value,
                    error_code="comment_submit_uncertain", revision=revision + 1, updated_at=now))
                if changed.rowcount != 1:
                    raise RevisionConflictError(assignment.id)
                self._append_recovery_attempt(session, assignment, now, "comment_submit_uncertain")
                self._pause_descendants_in_session(session, campaign.id, assignment.id, "comment_submit_uncertain", now)
                interrupted += 1
            for assignment in rows:
                if assignment.status != AssignmentStatus.AWAITING_STEP_APPROVAL.value:
                    continue
                consumed = session.scalar(select(CommentApprovalRecord.id).where(
                    CommentApprovalRecord.assignment_id == assignment.id,
                    CommentApprovalRecord.revision == assignment.revision,
                    CommentApprovalRecord.consumed_at != "",
                ))
                if consumed is None:
                    continue
                revision = assignment.revision
                changed = session.execute(update(CommentAssignmentRecord).where(
                    CommentAssignmentRecord.id == assignment.id,
                    CommentAssignmentRecord.revision == revision,
                    CommentAssignmentRecord.status == AssignmentStatus.AWAITING_STEP_APPROVAL.value,
                ).values(status=AssignmentStatus.WAITING_DEPENDENCY.value, evidence_json=_json({}),
                    error_code="approval_revision_mismatch", error_summary="",
                    revision=revision + 1, updated_at=now))
                if changed.rowcount != 1:
                    raise RevisionConflictError(assignment.id)
                self._append_recovery_attempt(session, assignment, now, "approval_revision_mismatch")
                approval_recovered += 1
            eligible = self._eligible_assignment_ids_in_session(session, campaign.id)
            generation = None
            # Only a recovery that created fresh eligible work receives a new
            # generation.  Existing g1 may already be complete in RQ and is not
            # a valid restart target for this newly recovered assignment.
            if (approval_recovered and eligible
                    and campaign.status in {CampaignStatus.QUEUED.value, CampaignStatus.RUNNING.value}):
                current_generation = int(campaign.prepare_generation)
                changed = session.execute(update(CommentCampaignRecord).where(
                    CommentCampaignRecord.id == campaign.id,
                    CommentCampaignRecord.prepare_generation == current_generation,
                ).values(prepare_generation=current_generation + 1, updated_at=now))
                if changed.rowcount != 1:
                    raise RevisionConflictError(campaign.id)
                generation = current_generation + 1
            return {
                "interrupted": interrupted,
                "approval_recovered": approval_recovered,
                "prepare_generation": generation,
            }

    @staticmethod
    def _eligible_assignment_ids_in_session(session, campaign_id: str) -> list[str]:
        rows = session.scalars(select(CommentAssignmentRecord).where(
            CommentAssignmentRecord.campaign_id == str(campaign_id),
            CommentAssignmentRecord.status.in_((AssignmentStatus.PLANNED.value, AssignmentStatus.WAITING_DEPENDENCY.value)),
        ).order_by(CommentAssignmentRecord.position, CommentAssignmentRecord.id)).all()
        eligible: list[str] = []
        for row in rows:
            if row.parent_assignment_id is None:
                eligible.append(row.id)
                continue
            parent = session.get(CommentAssignmentRecord, row.parent_assignment_id)
            if parent is None or parent.campaign_id != row.campaign_id or parent.status != AssignmentStatus.PUBLISHED_VERIFIED.value:
                continue
            receipt = session.scalar(select(CommentReceiptRecord.id).where(
                CommentReceiptRecord.assignment_id == parent.id,
                CommentReceiptRecord.status == AssignmentStatus.PUBLISHED_VERIFIED.value,
            ))
            if receipt is not None:
                eligible.append(row.id)
        return eligible

    @staticmethod
    def _append_recovery_attempt(session, assignment: CommentAssignmentRecord, now: str, error_code: str) -> None:
        attempt_no = int(session.scalar(select(func.coalesce(func.max(CommentAttemptRecord.attempt_no), 0)).where(
            CommentAttemptRecord.assignment_id == assignment.id
        )) or 0) + 1
        session.add(CommentAttemptRecord(
            id=f"attempt_{uuid4().hex}", campaign_id=assignment.campaign_id,
            assignment_id=assignment.id, profile_ref=assignment.profile_ref,
            attempt_no=attempt_no, stage="recovery", status="failed", error_code=error_code,
            error_summary="", evidence_paths_json=_json([]), started_at=now, finished_at=now,
        ))

    def recover_interrupted_campaigns(self) -> dict[str, dict[str, int]]:
        """Run the campaign-scoped atomic primitive for every interrupted campaign."""
        with self.session_factory.begin() as session:
            campaign_ids = list(session.scalars(select(CommentAssignmentRecord.campaign_id).where(
                CommentAssignmentRecord.status.in_((AssignmentStatus.SUBMITTING.value, AssignmentStatus.VERIFYING_RECEIPT.value, AssignmentStatus.AWAITING_STEP_APPROVAL.value))
            ).distinct()))
        return {campaign_id: self.reconcile_campaign_state(campaign_id) for campaign_id in campaign_ids}

    def recover_interrupted_submissions(self) -> int:
        """Compatibility worker-start count backed by the atomic primitive."""
        return sum(
            result["interrupted"] + result["approval_recovered"]
            for result in self.recover_interrupted_campaigns().values()
        )

    @staticmethod
    def _expected_revision(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expected_revision must be a positive integer")
        return value

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be non-negative")

    @staticmethod
    def _missing(identifier: str) -> Any:
        raise CampaignNotFoundError(str(identifier))

    @staticmethod
    def _display_profile(profile_ref: str, name: str) -> str:
        return name.strip() or f"Profile {profile_ref[-8:]}"

    @classmethod
    def _template_snapshot(cls, template_id: str, revision: int, data: dict, *, enabled: bool) -> dict:
        return {"id": str(template_id), "revision": revision, "name": data["name"], "description": data.get("description", ""), "supported_modes": list(data["supported_modes"]), "language": data.get("language", ""), "tags": list(data.get("tags", [])), "enabled": bool(enabled), "steps": [dict(step) for step in data["steps"]]}

    @classmethod
    def _add_template_revision(cls, session, template_id: str, revision: int, snapshot: dict, now: str) -> None:
        session.add(CommentTemplateRevision(template_id=str(template_id), revision=revision, snapshot_json=_json(snapshot), created_at=now))
        # Materialize the revision before inserting same-revision self-referencing
        # steps; SQLite checks the composite parent key immediately.
        session.flush()
        for position, step in enumerate(snapshot["steps"], start=1):
            session.add(CommentStepRecord(template_id=str(template_id), template_revision=revision, step_id=str(step["id"]), parent_step_id=step.get("parent_step_id"), position=position, definition_json=_json(step)))

    @staticmethod
    def _metadata_values(fields: dict[str, Any]) -> dict[str, Any]:
        return {"expected_username": str(fields.get("expected_username") or ""), "enabled": bool(fields.get("enabled")), "login_verified": bool(fields.get("login_verified")), "tags_json": _json(fields.get("tags", [])), "language": str(fields.get("language") or ""), "region": str(fields.get("region") or ""), "cooldown_until": str(fields.get("cooldown_until") or ""), "health_status": str(fields.get("health_status") or "unknown")}

    @staticmethod
    def _template_record(row: CommentTemplateRecord) -> dict:
        return {"id": row.id, "name": row.name, "description": row.description, "supported_modes": _loads(row.supported_modes_json, []), "language": row.language, "tags": _loads(row.tags_json, []), "enabled": bool(row.enabled), "revision": row.revision, "created_at": row.created_at, "updated_at": row.updated_at}

    @classmethod
    def _template_return(cls, snapshot: dict, *, enabled: bool, created_at: str, updated_at: str) -> dict:
        return {"id": snapshot["id"], "name": snapshot["name"], "description": snapshot["description"], "supported_modes": snapshot["supported_modes"], "language": snapshot["language"], "tags": snapshot["tags"], "enabled": enabled, "revision": snapshot["revision"], "steps": snapshot["steps"], "created_at": created_at, "updated_at": updated_at}

    @staticmethod
    def _campaign_record(row: CommentCampaignRecord) -> dict:
        return {"id": row.id, "name": row.name, "mode": row.mode, "target_source": row.target_source, "target_reference": row.target_reference, "video_id": row.video_id, "canonical_url": row.canonical_url, "template_id": row.template_id, "template_revision": row.template_revision, "profile_refs": _loads(row.profile_refs_json, []), "batch_size": row.batch_size, "allocation_seed": row.allocation_seed, "start_mode": row.start_mode, "scheduled_at": row.scheduled_at or None, "status": row.status, "pause_reason": row.pause_reason or None, "revision": row.revision, "prepare_generation": row.prepare_generation, "template_snapshot": _loads(row.template_snapshot_json, {}), "profile_snapshot": _loads(row.profile_snapshot_json, []), "content_snapshot": _loads(row.content_snapshot_json, []), "locked_at": row.locked_at or None, "created_at": row.created_at, "updated_at": row.updated_at}

    @staticmethod
    def _assignment_record(row: CommentAssignmentRecord) -> dict:
        return {"assignment_id": row.id, "campaign_id": row.campaign_id, "step_id": row.step_id, "profile_ref": row.profile_ref, "display_profile": row.display_profile, "expected_username": row.expected_username, "role": row.role, "resolved_text": row.resolved_text, "parent_assignment_id": row.parent_assignment_id, "position": row.position, "status": row.status, "revision": row.revision, "locked_at": row.locked_at or None, "error_code": row.error_code or None, "error_summary": row.error_summary or None, "evidence": _loads(row.evidence_json, {}), "created_at": row.created_at, "updated_at": row.updated_at}

    @staticmethod
    def _approval_record(row: CommentApprovalRecord) -> dict:
        return {"approval_id": row.id, "campaign_id": row.campaign_id, "assignment_id": row.assignment_id, "revision": row.revision, "approved_at": row.approved_at, "consumed_at": row.consumed_at or None}

    @staticmethod
    def _receipt_record(row: CommentReceiptRecord) -> dict:
        result = _loads(row.receipt_json, {})
        result.update({"receipt_id": row.id, "campaign_id": row.campaign_id, "assignment_id": row.assignment_id, "status": row.status, "created_at": row.created_at, "updated_at": row.updated_at})
        return result

    @staticmethod
    def _attempt_record(row: CommentAttemptRecord) -> dict:
        return {"attempt_id": row.id, "campaign_id": row.campaign_id, "assignment_id": row.assignment_id, "profile_ref": row.profile_ref, "attempt_no": row.attempt_no, "stage": row.stage, "status": row.status, "error_code": row.error_code or None, "error_summary": row.error_summary or None, "evidence_paths": _loads(row.evidence_paths_json, []), "started_at": row.started_at, "finished_at": row.finished_at or None}

    @staticmethod
    def _identity_public_record(row: CommentProfileIdentityRecord) -> dict:
        return {"profile_ref": row.profile_ref, "display_profile": row.display_profile, "name": row.name, "status": row.status}

    @staticmethod
    def _strict_raw_profile(value: Any) -> tuple[str, str, str]:
        if not isinstance(value, dict) or set(value) - {"id", "name", "status"}:
            raise ValueError("profile identity input is invalid")
        raw_id = value.get("id")
        name = value.get("name")
        status = value.get("status")
        if not isinstance(raw_id, str) or not raw_id or not isinstance(name, str) or not isinstance(status, str):
            raise ValueError("profile identity input is invalid")
        return raw_id, name, status

    @staticmethod
    def _metadata_record(row: CommentProfileMetadataRecord) -> dict:
        return {"profile_ref": row.profile_ref, "expected_username": row.expected_username, "enabled": bool(row.enabled), "login_verified": bool(row.login_verified), "tags": _loads(row.tags_json, []), "language": row.language, "region": row.region, "cooldown_until": row.cooldown_until or None, "health_status": row.health_status, "created_at": row.created_at, "updated_at": row.updated_at}
