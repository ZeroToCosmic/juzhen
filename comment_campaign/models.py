"""SQLAlchemy tables for the isolated Comment Campaign database."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CommentTemplateRecord(Base):
    __tablename__ = "comment_templates"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    supported_modes_json: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tags_json: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class CommentTemplateRevision(Base):
    __tablename__ = "comment_template_revisions"
    __table_args__ = (UniqueConstraint("template_id", "revision", name="uq_template_revision"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("comment_templates.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class CommentStepRecord(Base):
    __tablename__ = "comment_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "template_revision", "parent_step_id"],
            ["comment_steps.template_id", "comment_steps.template_revision", "comment_steps.step_id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["template_id", "template_revision"],
            ["comment_template_revisions.template_id", "comment_template_revisions.revision"],
            ondelete="CASCADE",
        ),
    )
    template_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    template_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    parent_step_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)


class CommentCampaignRecord(Base):
    __tablename__ = "comment_campaigns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "template_revision"],
            ["comment_template_revisions.template_id", "comment_template_revisions.revision"],
        ),
    )
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    target_source: Mapped[str] = mapped_column(String(32), nullable=False)
    target_reference: Mapped[str] = mapped_column(Text, nullable=False)
    video_id: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    template_id: Mapped[str] = mapped_column(String(120), nullable=False)
    template_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_refs_json: Mapped[str] = mapped_column(Text, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    allocation_seed: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    start_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    scheduled_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="draft")
    pause_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prepare_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Last generation durably handed to recovery.  Repeated restarts reuse the
    # same idempotent RQ job ID instead of making another prepare job.
    reconcile_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    template_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    profile_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    locked_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class CommentAssignmentRecord(Base):
    __tablename__ = "comment_assignments"
    __table_args__ = (
        UniqueConstraint("campaign_id", "step_id", name="uq_campaign_step"),
        UniqueConstraint("campaign_id", "profile_ref", name="uq_campaign_profile"),
        UniqueConstraint("campaign_id", "id", name="uq_campaign_assignment"),
        ForeignKeyConstraint(
            ["campaign_id", "parent_assignment_id"],
            ["comment_assignments.campaign_id", "comment_assignments.id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("comment_campaigns.id", ondelete="CASCADE"), nullable=False)
    step_id: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_ref: Mapped[str] = mapped_column(ForeignKey("comment_profile_identities.profile_ref"), nullable=False)
    display_profile: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    expected_username: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    resolved_text: Mapped[str] = mapped_column(Text, nullable=False)
    parent_assignment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="planned")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    locked_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    error_code: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    error_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class CommentApprovalRecord(Base):
    """One human approval for one immutable Assignment revision.

    The token is deliberately private: RQ receives only IDs and the revision.
    """

    __tablename__ = "comment_approvals"
    __table_args__ = (UniqueConstraint("assignment_id", "revision", name="uq_comment_approval_revision"),)
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("comment_campaigns.id", ondelete="CASCADE"), nullable=False)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("comment_assignments.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_token: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[str] = mapped_column(String(40), nullable=False)
    consumed_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")


class CommentReceiptRecord(Base):
    __tablename__ = "comment_receipts"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("comment_campaigns.id", ondelete="CASCADE"), nullable=False)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("comment_assignments.id", ondelete="CASCADE"), nullable=False, unique=True)
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class CommentAttemptRecord(Base):
    __tablename__ = "comment_attempts"
    __table_args__ = (UniqueConstraint("campaign_id", "assignment_id", "attempt_no", name="uq_campaign_assignment_attempt"),)
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("comment_campaigns.id", ondelete="CASCADE"), nullable=False)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("comment_assignments.id", ondelete="CASCADE"), nullable=False)
    profile_ref: Mapped[str] = mapped_column(String(80), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    error_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_paths_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    finished_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")


class CommentProfileIdentityRecord(Base):
    __tablename__ = "comment_profile_identities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_ref: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    raw_adspower_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_profile: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class CommentProfileMetadataRecord(Base):
    __tablename__ = "comment_profile_metadata"
    profile_ref: Mapped[str] = mapped_column(ForeignKey("comment_profile_identities.profile_ref"), primary_key=True)
    expected_username: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    login_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    cooldown_until: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    health_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
