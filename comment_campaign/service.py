"""Pure campaign planning orchestration; it deliberately never submits comments."""

from __future__ import annotations

from datetime import datetime, timezone
import asyncio
from secrets import token_urlsafe
from threading import Lock
from typing import Any, Callable, Mapping, Protocol, Sequence
from unicodedata import normalize
import re
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from adspower import AdsPowerDependencyError
from remote_actions.publication import PublicationActor

from .allocation import AllocationError, allocate, profile_matches, recommend_profiles, validate_template_tree
from .domain import CampaignStatus
from .errors import CampaignError, CampaignNotFoundError, CampaignValidationError, RevisionConflictError
from .schemas import CampaignCreate, ProfileSelectionPreview, TemplateCreate, TemplateImportCommit, TemplateUpdate
from .store import CampaignStore
from .template_import import import_tree_to_template, normalize_imported_tree, preview_comment_tree_workbook
from .video import TargetVideo, normalize_tiktok_video
from .queueing import RedisUnavailableError


class ContentResolver(Protocol):
    """Campaign content adapter: list one library's durable item candidates."""

    def __call__(self, content_library_id: str) -> Sequence[Mapping[str, str]]: ...


PublishResultResolver = Callable[[str], str]


class CommentCampaignService:
    def __init__(
        self,
        store: CampaignStore,
        *,
        publish_result_resolver: PublishResultResolver | None = None,
        content_resolver: ContentResolver | None = None,
        profile_provider: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        queue_coordinator: Any | None = None,
        executor: Any | None = None,
        settings_provider: Callable[[], Mapping[str, Any]] | None = None,
        settings_updater: Callable[[int, Mapping[str, str]], Mapping[str, Any]] | None = None,
        adspower_probe: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self._publish_result_resolver = publish_result_resolver
        self._content_resolver = content_resolver
        self._profile_provider = profile_provider
        self._queue_coordinator = queue_coordinator
        self._executor = executor
        self._settings_provider = settings_provider
        self._settings_updater = settings_updater
        self._adspower_probe = adspower_probe
        self._runtime_closeables: list[Any] = []
        self._profile_sync_lock = Lock()
        self._profile_sync_state = {"stale": True, "safe_reason": None}

    def list_templates(self) -> list[dict]:
        return self.store.list_templates()

    def close(self) -> None:
        try:
            close = getattr(self._executor, "close", None)
            if callable(close):
                close()
        finally:
            try:
                for resource in self._runtime_closeables:
                    close = getattr(resource, "close", None)
                    if callable(close):
                        close()
            finally:
                self.store.close()

    def prepare_campaign(
        self, campaign_id: str, prepare_generation: int, identity_generation: int,
    ) -> dict:
        """RQ entry point: prepare only the next durable, bounded batch."""
        if self._executor is None:
            raise CampaignValidationError("worker_unavailable")
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        stale = self._stale_prepare_generation(
            campaign, prepare_generation, identity_generation
        )
        if stale:
            return self._stale_prepare_result(campaign)
        if campaign["status"] not in {"queued", "running"}:
            return {"stale": False, "prepared": (), "failed": (), "close_confirmed": True}
        if campaign["status"] == "queued":
            campaign = self.store.transition_campaign_status(
                campaign_id, campaign["revision"], "running"
            )
        if self._account_preflight_required(campaign_id):
            return self._preflight_required_result(campaign)
        eligible = self.store.eligible_assignment_ids(campaign_id)
        if not eligible:
            return {"prepared": (), "failed": (), "close_confirmed": True}
        result = asyncio.run(self._executor.prepare_batch(
            campaign_id, eligible[:campaign["batch_size"]], identity_generation,
        ))
        return {"stale": False, "prepared": result.prepared, "failed": result.failed, "close_confirmed": result.close_confirmed}

    async def job_prepare_campaign(
        self, campaign_id: str, prepare_generation: int, identity_generation: int,
    ) -> dict:
        """Async RQ path; paired with ``aclose`` on the same asyncio.run loop."""
        if self._executor is None:
            raise CampaignValidationError("worker_unavailable")
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        stale = self._stale_prepare_generation(
            campaign, prepare_generation, identity_generation
        )
        if stale:
            return self._stale_prepare_result(campaign)
        if campaign["status"] not in {"queued", "running"}:
            return {"stale": False, "prepared": (), "failed": (), "close_confirmed": True}
        if campaign["status"] == "queued":
            campaign = self.store.transition_campaign_status(
                campaign_id, campaign["revision"], "running"
            )
        if self._account_preflight_required(campaign_id):
            preflight = await self._executor.preflight_campaign_identities(
                campaign_id, identity_generation
            )
            if not preflight.ready:
                return {
                    "stale": preflight.stale, "ready": False, "prepared": (),
                    "failed": (), "close_confirmed": True,
                    "identity_generation": preflight.identity_generation,
                }
            campaign = self._campaign(campaign_id)
            identity_generation = preflight.identity_generation
            if (
                campaign.get("status") != "running"
                or campaign.get("prepare_generation") != prepare_generation
                or campaign.get("identity_generation") != identity_generation
            ):
                return self._stale_prepare_result(campaign)
        eligible = self.store.eligible_assignment_ids(campaign_id)
        if not eligible:
            return {"prepared": (), "failed": (), "close_confirmed": True}
        result = await self._executor.prepare_batch(
            campaign_id, eligible[:campaign["batch_size"]], identity_generation
        )
        return {"stale": False, "prepared": result.prepared, "failed": result.failed, "close_confirmed": result.close_confirmed}

    def enqueue_next_prepare_generation(self, campaign_id: str) -> dict:
        coordinator = self._require_queue()
        generation = self.store.next_prepare_generation(campaign_id)
        campaign = self._campaign(campaign_id)
        job = self._enqueue_prepare_generation(
            coordinator, campaign_id, generation, campaign["identity_generation"]
        )
        return {"generation": generation, "job_id": self._job_id(job)}

    def reconcile_campaign(self, campaign_id: str) -> dict:
        """Recover uncertain local state and enqueue at most one prepare job.

        This path never creates submit work.  ``pending_reconcile_*`` returns an
        existing generation, so concurrent callers hand QueueCoordinator the
        same idempotent RQ ID rather than replaying a comment or making g+1.
        """
        recovery = self.store.reconcile_campaign_state(campaign_id)
        campaign = self.store.get_campaign(campaign_id)
        preflight_required = (
            campaign is not None and campaign.get("status") in {"queued", "running"}
            and self._account_preflight_required(campaign_id)
        )
        eligible = (
            self.store.eligible_assignment_ids(campaign_id)
            if campaign is not None and campaign.get("status") in {"queued", "running"}
            and not preflight_required
            else []
        )
        generation = None
        if preflight_required or eligible:
            generation = recovery.get("prepare_generation") or self.store.pending_reconcile_prepare_generation(campaign_id)
        enqueued = False
        job_id = None
        if generation is not None:
            coordinator = self._require_queue()
            campaign = self._campaign(campaign_id)
            job = self._enqueue_prepare_generation(
                coordinator, campaign_id, generation, campaign["identity_generation"]
            )
            # Mark after durable queue acceptance.  If the process dies before
            # this write, a later reconciliation repeats the same RQ job ID.
            self.store.mark_reconcile_prepare_generation(campaign_id, generation)
            enqueued, job_id = True, self._job_id(job)
        return {
            "campaign_id": campaign_id,
            "recovered": recovery["interrupted"] + recovery["approval_recovered"],
            "interrupted": recovery["interrupted"],
            "approval_recovered": recovery["approval_recovered"],
            "eligible_assignment_ids": tuple(eligible),
            "enqueued": enqueued,
            "generation": generation,
            "job_id": job_id,
        }

    def submit_assignment(self, campaign_id: str, assignment_id: str, revision: int) -> dict:
        if self._executor is None:
            raise CampaignValidationError("worker_unavailable")
        return asyncio.run(self._executor.submit_assignment(campaign_id, assignment_id, revision))

    async def job_submit_assignment(self, campaign_id: str, assignment_id: str, revision: int) -> dict:
        if self._executor is None:
            raise CampaignValidationError("worker_unavailable")
        return await self._executor.submit_assignment(campaign_id, assignment_id, revision)

    async def aclose(self) -> None:
        try:
            close = getattr(self._executor, "aclose", None)
            if callable(close):
                await close()
        finally:
            try:
                for resource in self._runtime_closeables:
                    close = getattr(resource, "close", None)
                    if callable(close):
                        value = close()
                        if hasattr(value, "__await__"):
                            await value
            finally:
                self.store.close()

    def get_template(self, template_id: str) -> dict | None:
        return self.store.get_template(template_id)

    def create_template(self, payload: TemplateCreate | dict[str, Any], template_id: str | None = None) -> dict:
        template = payload if isinstance(payload, TemplateCreate) else TemplateCreate.model_validate(payload)
        for mode in template.supported_modes:
            validate_template_tree(mode, template.steps, supported_modes=template.supported_modes)
        return self.store.create_template(template, template_id or str(uuid4()))

    def preview_template_import(self, filename: str, content: bytes) -> dict[str, Any]:
        return preview_comment_tree_workbook(filename, content)

    def import_templates(
        self, payload: TemplateImportCommit | Mapping[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        request = (
            payload
            if isinstance(payload, TemplateImportCommit)
            else TemplateImportCommit.model_validate(payload)
        )
        created: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for tree in request.trees:
            raw_tree = tree.model_dump()
            normalized = normalize_imported_tree(raw_tree)
            if not normalized["valid"]:
                rejected.append({"name": normalized["name"], "errors": normalized["errors"]})
                continue
            try:
                template = TemplateCreate.model_validate(import_tree_to_template(raw_tree))
                created.append(self.create_template(template))
            except IntegrityError:
                rejected.append({"name": normalized["name"], "errors": [{"code": "import_tree_failed"}]})
            except CampaignError as exc:
                rejected.append({"name": normalized["name"], "errors": [{"code": exc.code}]})
        return {"created": created, "rejected": rejected}

    def update_template(self, template_id: str, payload: TemplateUpdate | dict[str, Any]) -> dict:
        template = payload if isinstance(payload, TemplateUpdate) else TemplateUpdate.model_validate(payload)
        for mode in template.supported_modes:
            validate_template_tree(mode, template.steps, supported_modes=template.supported_modes)
        return self.store.update_template(template_id, template.expected_revision, template)

    def disable_template(self, template_id: str, expected_revision: int) -> dict:
        return self.store.disable_template(template_id, expected_revision)

    def enable_template(self, template_id: str, expected_revision: int) -> dict:
        return self.store.enable_template(template_id, expected_revision)

    def delete_template(self, template_id: str, expected_revision: int) -> dict:
        return self.store.delete_template(template_id, expected_revision)

    def upsert_profile_metadata(
        self, payload: dict[str, Any]
    ) -> dict:
        from .schemas import ProfileMetadataUpsert

        data = ProfileMetadataUpsert.model_validate(payload)
        return self.store.upsert_profile_metadata(**data.model_dump())

    def list_profile_metadata(self) -> dict:
        with self._profile_sync_lock:
            meta = dict(self._profile_sync_state)
        meta["last_synced_at"] = self.store.profile_cache_last_synced_at()
        return {"data": self.store.list_comment_profiles(), "meta": meta}

    def preview_profile_selection(
        self, payload: ProfileSelectionPreview | Mapping[str, Any],
    ) -> dict:
        request = (
            payload if isinstance(payload, ProfileSelectionPreview)
            else ProfileSelectionPreview.model_validate(payload)
        )
        current = self.store.get_template(request.template_id)
        template = self.store.get_template(
            request.template_id, revision=request.template_revision,
        )
        if (
            current is None or template is None or not current["enabled"]
            or request.mode not in template["supported_modes"]
        ):
            raise CampaignValidationError("template_invalid")
        eligibility_at = datetime.now(timezone.utc)
        profiles = self.store.list_comment_profiles()
        refs = recommend_profiles(
            template["steps"], profiles, eligibility_at=eligibility_at,
        )
        by_ref = {row["profile_ref"]: row for row in profiles}
        return {
            "required_count": len(template["steps"]),
            "eligible_count": sum(
                any(profile_matches(step, profile, eligibility_at=eligibility_at)
                    for step in template["steps"])
                for profile in profiles
            ),
            "profiles": [
                {"profile_ref": ref, "display_profile": by_ref[ref]["display_profile"]}
                for ref in refs
            ],
        }

    def sync_profile_metadata(self) -> dict:
        try:
            rows = self._validated_profile_provider_rows()
        except AdsPowerDependencyError as error:
            state = {"stale": True, "safe_reason": error.reason}
        else:
            self.store.sync_profile_identities(rows)
            state = {"stale": False, "safe_reason": None}
        with self._profile_sync_lock:
            self._profile_sync_state = state
        return self.list_profile_metadata()

    def _validated_profile_provider_rows(self) -> list[dict[str, str]]:
        if self._profile_provider is None:
            raise AdsPowerDependencyError("not_configured")
        raw_profiles = self._profile_provider()
        if not isinstance(raw_profiles, Sequence) or isinstance(raw_profiles, (str, bytes)):
            raise RuntimeError("profile provider returned invalid rows")
        profiles: list[dict[str, str]] = []
        for item in raw_profiles:
            if not isinstance(item, Mapping):
                raise RuntimeError("profile provider returned invalid rows")
            raw_id, name, status = item.get("id"), item.get("name"), item.get("status")
            if not isinstance(raw_id, str) or not raw_id or not isinstance(name, str) or not isinstance(status, str):
                raise RuntimeError("profile provider returned invalid rows")
            profiles.append({"id": raw_id, "name": name, "status": status})
        return profiles

    def list_campaigns(
        self, *, status: str | None, limit: int, offset: int
    ) -> list[dict]:
        return self.store.list_campaigns(status, limit, offset)

    def get_campaign(self, campaign_id: str) -> dict | None:
        return self.store.get_campaign(campaign_id)

    def delete_campaign(self, campaign_id: str, expected_revision: int) -> None:
        self.store.delete_campaign(campaign_id, expected_revision)

    def get_campaign_publication_metadata(self, campaign_id: str) -> dict:
        return self.store.get_action_publication_metadata(campaign_id)

    def begin_campaign_debug_run(self, campaign_id: str, *, run_id: str) -> dict:
        return self.store.begin_debug_run(campaign_id, run_id)

    def complete_campaign_debug_run(
        self, run_id: str, *, status: str, finished_at: str
    ) -> dict:
        return self.store.complete_debug_run(run_id, status, finished_at)

    def prepare_campaign_release(
        self,
        campaign_id: str,
        *,
        actor: PublicationActor,
        waive_validation: bool = False,
        reason: str = "",
    ) -> dict:
        metadata = self.store.get_action_publication_metadata(campaign_id)
        return self.store.prepare_release(
            metadata["action_id"],
            metadata["action_revision"],
            actor,
            waive_validation=waive_validation,
            reason=reason,
        )

    def mark_campaign_release_synced(
        self,
        action_id: str,
        release_revision: int,
        *,
        central_revision: int,
        synced_at: str,
    ) -> dict:
        return self.store.mark_release_synced(
            action_id,
            release_revision,
            central_revision,
            synced_at,
        )

    def get_campaign_detail(self, campaign_id: str) -> dict | None:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            return None
        return {
            "campaign": campaign,
            "assignments": self.store.list_assignments(campaign_id),
        }

    def override_assignment(
        self, campaign_id: str, assignment_id: str, payload: dict[str, Any]
    ) -> dict:
        from .schemas import AssignmentOverride

        data = AssignmentOverride.model_validate(payload)
        return self.store.override_assignment_profile(
            campaign_id,
            assignment_id,
            data.expected_revision,
            data.profile_ref,
        )

    def list_receipts(self, campaign_id: str) -> list[dict]:
        if self.store.get_campaign(campaign_id) is None:
            raise CampaignNotFoundError(campaign_id)
        return self.store.list_receipts(campaign_id)

    def list_attempts(self, campaign_id: str) -> list[dict]:
        if self.store.get_campaign(campaign_id) is None:
            raise CampaignNotFoundError(campaign_id)
        return self.store.list_attempts(campaign_id)

    def pause_descendants(self, campaign_id: str, parent_assignment_id: str, error_code: str) -> list[str]:
        if self.store.get_campaign(campaign_id) is None:
            raise CampaignNotFoundError(campaign_id)
        return self.store.pause_descendants(campaign_id, parent_assignment_id, error_code)

    def resume_verified_children(self, campaign_id: str, parent_assignment_id: str) -> list[str]:
        changed = self.store.resume_verified_children(campaign_id, parent_assignment_id)
        campaign = self.store.get_campaign(campaign_id)
        if changed and campaign and campaign.get("status") == "running" and self._queue_coordinator is not None:
            enqueue = getattr(self._queue_coordinator, "enqueue_prepare_generation", None)
            if callable(enqueue):
                generation = self.store.next_prepare_generation(campaign_id)
                campaign = self._campaign(campaign_id)
                self._enqueue_prepare_generation(
                    self._queue_coordinator, campaign_id, generation,
                    campaign["identity_generation"],
                )
        return changed

    def create_campaign(self, payload: CampaignCreate | dict[str, Any], campaign_id: str | None = None) -> dict:
        campaign = payload if isinstance(payload, CampaignCreate) else CampaignCreate.model_validate(payload)
        current_template = self.store.get_template(campaign.template_id)
        selected_template = self.store.get_template(
            campaign.template_id,
            revision=campaign.template_revision,
        )
        if (
            current_template is None
            or selected_template is None
            or not current_template["enabled"]
            or campaign.mode not in selected_template["supported_modes"]
        ):
            raise CampaignValidationError("template_invalid")
        self._profile_snapshot(
            campaign.profile_refs, datetime.now(timezone.utc),
            required_count=len(selected_template["steps"]),
            steps=selected_template["steps"],
        )
        target = self._resolve_target(campaign.target_source, campaign.target_reference)
        data = campaign.model_copy(update={"allocation_seed": campaign.allocation_seed or token_urlsafe(24)})
        return self.store.create_campaign(data, campaign_id or str(uuid4()), target.video_id, target.canonical_url)

    def plan_campaign(self, campaign_id: str, seed: str | None = None, *, expected_revision: int | None = None) -> dict:
        """Make a complete plan in memory, then persist it with one CAS transaction."""
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        if expected_revision is not None and campaign["revision"] != expected_revision:
            raise RevisionConflictError(campaign_id)
        if campaign["status"] not in {CampaignStatus.DRAFT.value, CampaignStatus.PLANNED.value}:
            raise RevisionConflictError(campaign_id)
        self._require_unlocked_template_available(campaign)
        target = self._resolve_target(campaign["target_source"], campaign["target_reference"])
        if target.video_id != campaign["video_id"] or target.canonical_url != campaign["canonical_url"]:
            raise CampaignValidationError("target_video_invalid")
        current_template = self.store.get_template(campaign["template_id"])
        template = self.store.get_template(campaign["template_id"], revision=campaign["template_revision"])
        if current_template is None or not current_template["enabled"]:
            raise CampaignValidationError("template_unavailable")
        if template is None:
            raise CampaignValidationError("template_invalid")
        steps = template["steps"]
        validate_template_tree(campaign["mode"], steps, supported_modes=template["supported_modes"])
        eligibility_at = datetime.now(timezone.utc)
        profile_snapshot = self._profile_snapshot(
            campaign["profile_refs"], eligibility_at, required_count=len(steps),
            steps=steps,
        )
        selected_seed = str(seed if seed is not None else campaign["allocation_seed"])
        if not selected_seed:
            selected_seed = token_urlsafe(24)
        content_snapshot, texts = self._freeze_content(steps, selected_seed)
        planned = allocate(steps, profile_snapshot, texts, selected_seed, mode=campaign["mode"], campaign_id=campaign["id"], eligibility_at=eligibility_at)
        return self.store.replace_campaign_plan(
            campaign["id"], campaign["revision"], [item.as_dict() for item in planned],
            allocation_seed=selected_seed, template_snapshot=template,
            profile_snapshot=profile_snapshot, content_snapshot=content_snapshot,
        )

    def reallocate_campaign(self, campaign_id: str, seed: str | None = None, *, expected_revision: int | None = None) -> dict:
        """Reallocation is available only while the campaign has not been locked."""
        return self.plan_campaign(campaign_id, seed, expected_revision=expected_revision)

    def lock_plan(self, campaign_id: str, expected_revision: int | None = None) -> dict:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        self._require_unlocked_template_available(campaign)
        revision = campaign["revision"] if expected_revision is None else expected_revision
        return self.store.lock_campaign_plan(campaign_id, revision)

    def approve_campaign(self, campaign_id: str, expected_revision: int) -> dict:
        """Persist campaign queue intent before handing it to the background queue."""
        current = self.store.get_campaign(campaign_id)
        if current is None:
            raise CampaignNotFoundError(campaign_id)
        self._require_unlocked_template_available(current)
        coordinator = self._require_queue()
        campaign = self.store.approve_campaign_for_queue(campaign_id, expected_revision)
        try:
            if campaign["start_mode"] == "scheduled":
                scheduled_at = datetime.fromisoformat(str(campaign["scheduled_at"]).replace("Z", "+00:00"))
                generation = self.store.next_prepare_generation(campaign_id)
                campaign = self._campaign(campaign_id)
                job = coordinator.enqueue_at(
                    campaign_id, scheduled_at, generation,
                    campaign["identity_generation"],
                )
            else:
                generation = self.store.next_prepare_generation(campaign_id)
                campaign = self._campaign(campaign_id)
                job = self._enqueue_prepare_generation(
                    coordinator, campaign_id, generation,
                    campaign["identity_generation"],
                )
        except Exception as exc:
            raise self._queue_error(exc) from exc
        return {"campaign": campaign, "job_id": self._job_id(job)}

    def _require_unlocked_template_available(self, campaign: Mapping[str, Any]) -> None:
        """Reject unavailable trees until a Campaign has frozen its plan."""
        if campaign.get("locked_at"):
            return
        if self.store.get_template_lifecycle(str(campaign["template_id"])) != "enabled":
            raise CampaignValidationError("template_unavailable")

    def approve_submit(self, campaign_id: str, assignment_id: str, expected_revision: int) -> dict:
        """Persist an opaque, one-time approval before creating a submit job."""
        approval = self.store.create_submit_approval(
            campaign_id, assignment_id, expected_revision, token_urlsafe(32)
        )
        try:
            job = self._require_queue().enqueue_submit(campaign_id, assignment_id, expected_revision)
        except Exception as exc:
            # The approval remains durable and unconsumed; retrying this exact
            # revision safely reuses it and queue-level idempotency prevents duplicates.
            raise self._queue_error(exc) from exc
        return {"approval": approval, "job_id": self._job_id(job)}

    def reject_submit(
        self, campaign_id: str, assignment_id: str, expected_revision: int, reason: str
    ) -> dict:
        """The operator can stop this Assignment and its branch before click."""
        return self.store.reject_submit_and_pause_descendants(
            campaign_id, assignment_id, expected_revision, reason
        )

    def resolve_unverified(
        self, campaign_id: str, assignment_id: str, expected_revision: int,
        resolution: str, reason: str,
    ) -> dict:
        """Record an operator conclusion; this method never enqueues a submit."""
        assignment = self.store.resolve_unverified_assignment(
            campaign_id, assignment_id, expected_revision, resolution, reason
        )
        if resolution == "published":
            self.resume_verified_children(campaign_id, assignment_id)
            return assignment
        try:
            coordinator = self._require_queue()
            generation = self.store.next_prepare_generation(campaign_id)
            campaign = self._campaign(campaign_id)
            job = self._enqueue_prepare_generation(
                coordinator, campaign_id, generation, campaign["identity_generation"]
            )
        except Exception as exc:
            raise self._queue_error(exc) from exc
        return {"assignment": assignment, "job_id": self._job_id(job)}

    def pause_campaign(
        self, campaign_id: str, expected_revision: int, reason: str
    ) -> dict:
        return self.store.transition_campaign_status(
            campaign_id, expected_revision, CampaignStatus.PAUSED.value,
            pause_reason=reason,
        )

    def resume_campaign(self, campaign_id: str, expected_revision: int) -> dict:
        campaign = self.store.transition_campaign_status(
            campaign_id, expected_revision, CampaignStatus.QUEUED.value
        )
        try:
            coordinator = self._require_queue()
            generation = self.store.next_prepare_generation(campaign_id)
            campaign = self._campaign(campaign_id)
            job = self._enqueue_prepare_generation(
                coordinator, campaign_id, generation, campaign["identity_generation"]
            )
        except Exception as exc:
            raise self._queue_error(exc) from exc
        return {"campaign": campaign, "job_id": self._job_id(job)}

    def cancel_campaign(self, campaign_id: str, expected_revision: int) -> dict:
        return self.store.transition_campaign_status(
            campaign_id, expected_revision, CampaignStatus.CANCELLED.value
        )

    def health(self) -> dict:
        """Probe SQLite, Redis, worker heartbeat, and AdsPower independently."""
        try:
            self.store.list_campaigns(None, 1, 0)
            sqlite = {"status": "connected"}
        except Exception:
            sqlite = {"status": "unavailable", "message": "SQLite 不可用"}
        redis_client = None
        try:
            if self._queue_coordinator is None:
                raise RuntimeError("unwired")
            redis_client = self._queue_coordinator.redis
            if not redis_client.ping():
                raise RuntimeError("ping failed")
            redis = {"status": "connected"}
        except Exception:
            redis = {"status": "unavailable", "message": "Redis 不可用"}
        try:
            if redis_client is None:
                raise RuntimeError("redis unavailable")
            from .worker import WORKER_HEALTH_KEY, WORKER_HEALTH_VALUE
            owner = redis_client.get(WORKER_HEALTH_KEY)
            if isinstance(owner, bytes):
                owner = owner.decode("utf-8", "strict")
            ttl = redis_client.ttl(WORKER_HEALTH_KEY)
            if not isinstance(owner, str) or not owner.startswith(WORKER_HEALTH_VALUE + ":") or not isinstance(ttl, int) or ttl <= 0:
                raise RuntimeError("worker heartbeat missing")
            worker = {"status": "connected"}
        except Exception:
            worker = {"status": "unavailable", "message": "Worker 心跳不可用"}
        try:
            probe = self._adspower_probe
            if probe is None:
                raise RuntimeError("unwired")
            profiles = probe()
            if (
                not isinstance(profiles, Mapping)
                or profiles.get("status") not in {"connected", "unavailable"}
                or profiles.get("reason") not in {
                    "connected", "timeout", "connection_refused",
                    "authentication_failed", "invalid_response", "not_configured",
                }
            ):
                raise RuntimeError("invalid probe")
            adspower = {
                "status": profiles["status"],
                "reason": profiles["reason"],
            }
        except Exception:
            adspower = {"status": "unavailable", "message": "AdsPower 不可用"}
        if (
            adspower.get("status") not in {"connected", "unavailable"}
            or adspower.get("reason") not in {
                "connected", "timeout", "connection_refused",
                "authentication_failed", "invalid_response", "not_configured",
            }
            or (adspower.get("status") == "connected" and adspower.get("reason") != "connected")
            or (adspower.get("status") == "unavailable" and adspower.get("reason") == "connected")
        ):
            adspower = {"status": "unavailable", "reason": "invalid_response"}
        return {"sqlite": sqlite, "redis": redis, "worker": worker, "adspower": adspower}

    def get_comment_settings(self) -> dict:
        """Expose only the four opaque V2 binding IDs used by the worker."""
        raw = self._settings_provider() if self._settings_provider is not None else {}
        bindings = raw.get("element_bindings", {}) if isinstance(raw, Mapping) else {}
        if not isinstance(bindings, Mapping):
            bindings = {}
        names = (
            "entry_element_id", "input_element_id", "submit_element_id",
            "account_element_id",
        )
        public = {
            name: str(bindings.get(name) or "")
            for name in names
        }
        return {
            "element_bindings": public,
            "revision": self._settings_revision(raw),
            "configured": all(public.values()),
            "can_write": self._settings_updater is not None,
        }

    @staticmethod
    def _settings_revision(raw: Mapping[str, Any] | Any) -> int:
        value = raw.get("revision", 1) if isinstance(raw, Mapping) else 1
        return value if type(value) is int and value >= 1 else 1

    def update_comment_settings(self, payload: Mapping[str, Any]) -> dict:
        if self._settings_updater is None:
            raise CampaignValidationError("comment_panel_not_ready")
        expected = payload.get("expected_revision")
        if type(expected) is not int or expected < 1:
            raise ValueError("expected_revision is invalid")
        names = (
            "entry_element_id", "input_element_id", "submit_element_id",
            "account_element_id",
        )
        bindings = {name: payload.get(name) for name in names}
        if any(not isinstance(value, str) or not value.strip() for value in bindings.values()):
            raise ValueError("comment element binding is invalid")
        saved = self._settings_updater(expected, bindings)
        if not isinstance(saved, Mapping):
            raise CampaignValidationError("comment_panel_not_ready")
        return self.get_comment_settings_from(saved)

    def get_comment_settings_from(self, raw: Mapping[str, Any]) -> dict:
        bindings = raw.get("element_bindings", {}) if isinstance(raw, Mapping) else {}
        if not isinstance(bindings, Mapping):
            bindings = {}
        names = ("entry_element_id", "input_element_id", "submit_element_id", "account_element_id")
        public = {name: str(bindings.get(name) or "") for name in names}
        return {"element_bindings": public, "revision": self._settings_revision(raw), "configured": all(public.values()), "can_write": self._settings_updater is not None}

    def list_approvals(self, campaign_id: str) -> list[dict]:
        if self.store.get_campaign(campaign_id) is None:
            raise CampaignNotFoundError(campaign_id)
        return self.store.list_approvals(campaign_id)

    def _require_queue(self):
        if self._queue_coordinator is None:
            raise CampaignValidationError("worker_unavailable")
        return self._queue_coordinator

    @staticmethod
    def _queue_error(exc: Exception) -> CampaignValidationError:
        if isinstance(exc, RedisUnavailableError):
            return CampaignValidationError("redis_unavailable")
        return CampaignValidationError("worker_unavailable")

    @staticmethod
    def _job_id(job: Any) -> str:
        if isinstance(job, Mapping):
            value = job.get("id")
        else:
            value = getattr(job, "id", None)
        if not isinstance(value, str) or not value:
            raise CampaignValidationError("worker_unavailable")
        return value

    def _enqueue_prepare_generation(
        self, coordinator: Any, campaign_id: str, generation: int,
        identity_generation: int,
    ) -> Any:
        """Queue first, then durably mark that generation as safely accepted."""
        job = coordinator.enqueue_prepare_generation(
            campaign_id, generation, identity_generation
        )
        marker = getattr(self.store, "mark_reconcile_prepare_generation", None)
        if callable(marker):
            marker(campaign_id, generation)
        return job

    @staticmethod
    def _stale_prepare_generation(
        campaign: Mapping[str, Any], prepare_generation: int, identity_generation: int,
    ) -> bool:
        if (
            type(prepare_generation) is not int or prepare_generation < 1
            or type(identity_generation) is not int or identity_generation < 0
        ):
            raise ValueError("prepare job generations are invalid")
        return (
            campaign.get("prepare_generation") != prepare_generation
            or campaign.get("identity_generation") != identity_generation
        )

    @staticmethod
    def _stale_prepare_result(campaign: Mapping[str, Any]) -> dict:
        return {
            "stale": True, "prepared": (), "failed": (), "close_confirmed": True,
            "identity_generation": campaign.get("identity_generation"),
        }

    @staticmethod
    def _preflight_required_result(campaign: Mapping[str, Any]) -> dict:
        return {
            "stale": False, "ready": False, "prepared": (), "failed": (),
            "close_confirmed": True, "identity_generation": campaign["identity_generation"],
        }

    def _account_preflight_required(self, campaign_id: str) -> bool:
        required = getattr(self.store, "account_preflight_required", None)
        return bool(required(campaign_id)) if callable(required) else False

    def _campaign(self, campaign_id: str) -> dict:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        return campaign

    def _resolve_target(self, source: str, reference: str) -> TargetVideo:
        if source == "manual_url":
            return normalize_tiktok_video(reference)
        if source == "publish_result" and self._publish_result_resolver is not None:
            return normalize_tiktok_video(self._publish_result_resolver(reference))
        raise CampaignValidationError("target_video_invalid")

    def _profile_snapshot(
        self, profile_refs: list[str], eligibility_at: datetime, *,
        required_count: int, steps: Sequence[Mapping[str, Any]],
    ) -> list[dict]:
        profiles = self.store.list_comment_profiles()
        by_ref = {str(profile["profile_ref"]): dict(profile) for profile in profiles}
        selected = [by_ref[profile_ref] for profile_ref in profile_refs if profile_ref in by_ref]
        missing = [profile_ref for profile_ref in profile_refs if profile_ref not in by_ref]
        if missing:
            raise AllocationError(
                "unknown_profile_ref", required_count=required_count,
                eligible_count=sum(
                    any(profile_matches(step, profile, eligibility_at=eligibility_at) for step in steps)
                    for profile in selected
                ),
            )
        return selected

    def _freeze_content(self, steps: list[dict], seed: str) -> tuple[dict[str, list[dict]], dict[str, dict[str, str]]]:
        """Read every library once and deterministically choose non-duplicate copy."""
        cache: dict[str, list[dict[str, str]]] = {}
        candidate_count = 0
        for step in steps:
            if step["content_source"] != "library":
                continue
            library_id = str(step["content_library_id"])
            if library_id in cache:
                continue
            if self._content_resolver is None:
                raise CampaignValidationError("content_library_unavailable")
            try:
                raw_items = self._content_resolver(library_id)
            except Exception as exc:
                raise CampaignValidationError("content_library_unavailable") from exc
            if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
                raise CampaignValidationError("content_library_unavailable")
            items: list[dict[str, str]] = []
            seen_ids: set[str] = set()
            if len(raw_items) > 300:
                raise CampaignValidationError("content_library_unavailable")
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    raise CampaignValidationError("content_library_unavailable")
                raw_item_id, raw_text = raw.get("content_item_id"), raw.get("text")
                if not isinstance(raw_item_id, str) or not isinstance(raw_text, str):
                    raise CampaignValidationError("content_library_unavailable")
                item_id, text = raw_item_id, raw_text
                if not item_id or len(item_id) > 120 or not text or len(text) > 2200 or item_id in seen_ids:
                    raise CampaignValidationError("content_library_unavailable")
                seen_ids.add(item_id)
                items.append({"content_item_id": item_id, "text": text})
            if not items:
                raise CampaignValidationError("content_library_unavailable")
            candidate_count += len(items)
            if candidate_count > 3000:
                raise CampaignValidationError("content_library_unavailable")
            cache[library_id] = sorted(items, key=lambda item: item["content_item_id"])

        candidates: dict[str, list[dict[str, str]]] = {}
        for step in steps:
            step_id, source = str(step["id"]), step["content_source"]
            if source == "fixed":
                text = str(step["fixed_text"])
                if not text:
                    raise CampaignValidationError("template_invalid")
                candidates[step_id] = [{"content_item_id": "", "text": text}]
                continue
            library_id = str(step["content_library_id"])
            preferred = str(step.get("content_item_id") or "")
            values = [item for item in cache[library_id] if not preferred or item["content_item_id"] == preferred]
            if not values:
                raise CampaignValidationError("content_library_unavailable")
            candidates[step_id] = values

        def text_key(value: str) -> str:
            return re.sub(r"\s+", " ", normalize("NFKC", value).strip())

        def candidate_key(step_id: str, item: Mapping[str, str]) -> str:
            import hashlib
            return hashlib.sha256(f"{seed}\0{step_id}\0{item['content_item_id']}\0{item['text']}".encode()).hexdigest()

        matched: dict[str, str] = {}
        selected: dict[str, dict[str, str]] = {}
        ordered = sorted(steps, key=lambda step: (len(candidates[str(step["id"])]), str(step["id"])))

        def assign(step_id: str, seen: set[str]) -> bool:
            for item in sorted(candidates[step_id], key=lambda item: candidate_key(step_id, item)):
                key = text_key(item["text"])
                if not key or key in seen:
                    continue
                seen.add(key)
                existing = matched.get(key)
                if existing is None or assign(existing, seen):
                    matched[key] = step_id
                    selected[step_id] = item
                    return True
            return False

        for step in ordered:
            if not assign(str(step["id"]), set()):
                raise AllocationError()
        step_snapshot = []
        for step in steps:
            step_id, item = str(step["id"]), selected[str(step["id"])]
            step_snapshot.append({"step_id": step_id, "content_source": step["content_source"], "content_library_id": str(step.get("content_library_id") or ""), "content_item_id": item["content_item_id"], "resolved_text": item["text"]})
        library_snapshot = [{"content_library_id": library_id, "items": items} for library_id, items in sorted(cache.items())]
        return {"libraries": library_snapshot, "steps": step_snapshot}, {step_id: {"content_item_id": item["content_item_id"], "text": item["text"]} for step_id, item in selected.items()}


def create_default_comment_campaign_service(
    *,
    database_url: str,
    profile_provider: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    content_resolver: ContentResolver | None = None,
    publish_result_resolver: PublishResultResolver | None = None,
    queue_coordinator: Any | None = None,
    settings_provider: Callable[[], Mapping[str, Any]] | None = None,
    settings_updater: Callable[[int, Mapping[str, str]], Mapping[str, Any]] | None = None,
    adspower_probe: Callable[[], Any] | None = None,
    runtime_closeables: Sequence[Any] = (),
) -> CommentCampaignService:
    """Build and initialize the durable, non-executing campaign service."""

    store = CampaignStore(database_url)
    store.initialize()
    service = CommentCampaignService(
        store,
        profile_provider=profile_provider,
        content_resolver=content_resolver,
        publish_result_resolver=publish_result_resolver,
        queue_coordinator=queue_coordinator,
        settings_provider=settings_provider,
        settings_updater=settings_updater,
        adspower_probe=adspower_probe,
    )
    service._runtime_closeables.extend(runtime_closeables)
    return service
