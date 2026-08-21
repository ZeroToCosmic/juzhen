"""Short-lived preparation and one-shot, human-approved campaign submission."""

from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from actions_dom import human_type
from execution_v2.actions import _normalize_input_text, _read_input_value

from .errors import (CampaignNotFoundError, CampaignValidationError,
                     DuplicateTikTokAccountError, RevisionConflictError)
from .locator import (locate_comment_input, locate_parent_comment, locate_submit_control, open_scoped_reply, open_comment_panel,
                      read_tiktok_identity, verify_logged_in_username, verify_video)
from .profile_gateway import IdentityPreflightStale
from .receipts import build_receipt, evidence_filename, normalize_comment_text, verify_receipt_candidates


@dataclass(frozen=True, slots=True)
class BatchResult:
    prepared: tuple[str, ...]
    failed: tuple[str, ...] = ()
    close_confirmed: bool = True


@dataclass(frozen=True, slots=True)
class PreflightResult:
    stale: bool
    ready: bool
    identity_generation: int


@dataclass(frozen=True, slots=True)
class _IdentityInvalidationOutcome:
    stale: bool
    identity_generation: int


class _IdentityGenerationStopped(RuntimeError):
    def __init__(self, outcome: _IdentityInvalidationOutcome) -> None:
        self.outcome = outcome
        super().__init__("identity generation stopped")


PREFLIGHT_FAILURE_CODES = frozenset({
    "profile_start_failed", "cdp_connect_failed", "adspower_unavailable",
    "redis_unavailable", "target_video_mismatch", "tiktok_login_required",
    "tiktok_identity_unavailable", "profile_close_failed",
})

RUNTIME_IDENTITY_FAILURE_CODES = PREFLIGHT_FAILURE_CODES | frozenset({"tiktok_identity_changed"})


class _LeaseHeartbeat:
    def __init__(self, refresh: Callable[[], Any], *, interval_seconds: float = 20) -> None:
        self._refresh, self._interval, self.lost = refresh, interval_seconds, False
        self.error: Exception | None = None
        self._task: asyncio.Task[Any] | None = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_args):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                value = self._refresh()
                if inspect.isawaitable(value): value = await value
                if not value:
                    self.lost = True
                    return
            except Exception as error:
                self.error = error
                self.lost = True
                return


class CommentExecutor:
    """This object owns no durable state: Store remains the source of truth."""

    def __init__(self, store: Any, gateway: Any, locator_resolver: Any, *,
                 element_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
                 settings_provider: Callable[[dict], Mapping[str, Any]] | None = None,
                 lease_factory: Callable[[str], Any] | None = None,
                 receipt_candidate_provider: Callable[[Any, dict, dict], Any] | None = None,
                 evidence_dir: str | Path = "data/comment_campaign/evidence",
                 queue_coordinator: Any | None = None) -> None:
        self.store, self.gateway, self.resolver = store, gateway, locator_resolver
        self._element_provider, self._settings_provider = element_provider, settings_provider
        self._lease_factory, self._receipt_candidates, self._evidence_dir, self._queue = lease_factory, receipt_candidate_provider, Path(evidence_dir), queue_coordinator

    def close(self) -> None:
        asyncio.run(self.gateway.close())

    async def aclose(self) -> None:
        await self.gateway.close()

    async def preflight_campaign_identities(
        self, campaign_id: str, expected_identity_generation: int,
    ) -> PreflightResult:
        campaign = self._campaign(campaign_id)
        if campaign.get("identity_generation") != expected_identity_generation:
            return PreflightResult(True, False, campaign["identity_generation"])
        start_revision = campaign["revision"]
        try:
            await self.gateway.acquire_campaign_lease(campaign_id)
        except IdentityPreflightStale:
            latest = self._campaign(campaign_id)
            return PreflightResult(True, False, latest["identity_generation"])
        except CampaignValidationError as error:
            if error.code != "redis_unavailable":
                raise
            assignments = [row for row in self.store.list_assignments(campaign_id) if row.get("status") not in {
                "published_verified", "published_unverified", "failed", "cancelled",
            }]
            if not assignments:
                return PreflightResult(False, True, expected_identity_generation)
            return self._preflight_from_invalidation(self._invalidate_preflight(
                campaign_id, start_revision, expected_identity_generation,
                "redis_unavailable", [row["assignment_id"] for row in assignments],
            ))
        try:
            assignments = [row for row in self.store.list_assignments(campaign_id) if row.get("status") not in {
                "published_verified", "published_unverified", "failed", "cancelled",
            }]
            if not assignments:
                return PreflightResult(False, True, expected_identity_generation)
            affected_ids: list[str] = [row["assignment_id"] for row in assignments[:int(campaign.get("batch_size", 3))]]
            observations: list[dict] = []
            open_refs: list[str] = []
            settings = self._settings(campaign)
            account = self._definition(settings, "account", "click")
            element_id = settings.get("account_element_id")
            element = self._element_provider(element_id) if self._element_provider and isinstance(element_id, str) else {}
            binding = {
                "id": element_id, "revision": element.get("revision") if isinstance(element, Mapping) else None,
                "definition_sha256": hashlib.sha256(json.dumps(account, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            }
            batch_size = int(campaign.get("batch_size", 3))
            heartbeat = _LeaseHeartbeat(
                lambda: self.gateway.refresh_leases(open_refs, campaign_id=campaign_id),
                interval_seconds=1,
            )
            await heartbeat.__aenter__()
            try:
                for offset in range(0, len(assignments), batch_size):
                    if heartbeat.lost:
                        if heartbeat.error is not None:
                            if isinstance(heartbeat.error, CampaignValidationError):
                                raise heartbeat.error
                            raise CampaignValidationError("redis_unavailable") from heartbeat.error
                        latest = self._campaign(campaign_id)
                        return PreflightResult(True, False, latest["identity_generation"])
                    latest = self._campaign(campaign_id)
                    if latest.get("status") != "running" or latest.get("identity_generation") != expected_identity_generation:
                        return PreflightResult(True, False, latest["identity_generation"])
                    batch = assignments[offset:offset + batch_size]
                    affected_ids = [row["assignment_id"] for row in batch]
                    bindings = await self.gateway.open_identity_batch(
                        [row["profile_ref"] for row in batch], campaign_id, expected_identity_generation
                    )
                    open_refs[:] = [row["profile_ref"] for row in batch]
                    batch_error: Exception | None = None
                    try:
                        for assignment, profile in zip(batch, bindings, strict=True):
                            if heartbeat.lost:
                                if heartbeat.error is not None:
                                    if isinstance(heartbeat.error, CampaignValidationError):
                                        raise heartbeat.error
                                    raise CampaignValidationError("redis_unavailable") from heartbeat.error
                                raise IdentityPreflightStale()
                            target = await verify_video(profile.page, campaign["video_id"])
                            if heartbeat.lost:
                                if heartbeat.error is not None:
                                    if isinstance(heartbeat.error, CampaignValidationError):
                                        raise heartbeat.error
                                    raise CampaignValidationError("redis_unavailable") from heartbeat.error
                                raise IdentityPreflightStale()
                            identity = await read_tiktok_identity(profile.page, account, resolver=self.resolver)
                            observations.append({
                                "assignment_id": assignment["assignment_id"], "profile_ref": assignment["profile_ref"],
                                **identity.as_dict(), "target_video": target, "element_binding": binding,
                            })
                    except Exception as error:
                        batch_error = error
                    finally:
                        closed = await self.gateway.close_bindings(bindings)
                        open_refs.clear()
                    if not all(closed.values()):
                        raise CampaignValidationError("profile_close_failed")
                    if batch_error is not None:
                        raise batch_error
                    if heartbeat.lost:
                        if heartbeat.error is not None:
                            if isinstance(heartbeat.error, CampaignValidationError):
                                raise heartbeat.error
                            raise CampaignValidationError("redis_unavailable") from heartbeat.error
                        latest = self._campaign(campaign_id)
                        return PreflightResult(True, False, latest["identity_generation"])
                    if not await self.gateway.refresh_campaign_lease(campaign_id):
                        raise IdentityPreflightStale()
            finally:
                await heartbeat.__aexit__(None, None, None)
            latest = self._campaign(campaign_id)
            if latest.get("status") != "running" or latest.get("identity_generation") != expected_identity_generation:
                return PreflightResult(True, False, latest["identity_generation"])
            try:
                frozen = self.store.freeze_campaign_identities(
                    campaign_id, start_revision, expected_identity_generation, tuple(observations)
                )
            except DuplicateTikTokAccountError as error:
                return self._preflight_from_invalidation(self._invalidate_preflight(campaign_id, start_revision, expected_identity_generation,
                    "duplicate_tiktok_account", list(error.assignment_ids), {"visible_username": error.visible_username}))
            except RevisionConflictError:
                latest = self._campaign(campaign_id)
                return PreflightResult(True, False, latest["identity_generation"])
            return PreflightResult(False, True, frozen["identity_generation"])
        except IdentityPreflightStale:
            latest = self._campaign(campaign_id)
            return PreflightResult(True, False, latest["identity_generation"])
        except CampaignValidationError as error:
            if error.code not in PREFLIGHT_FAILURE_CODES:
                raise
            return self._preflight_from_invalidation(self._invalidate_preflight(campaign_id, start_revision, expected_identity_generation,
                error.code, affected_ids))
        finally:
            await self.gateway.release_campaign_lease(campaign_id)

    def _invalidate_preflight(self, campaign_id: str, revision: int, generation: int,
                               code: str, assignment_ids: list[str], details: dict | None = None) -> _IdentityInvalidationOutcome:
        try:
            campaign = self.store.invalidate_campaign_identity(
                campaign_id, revision, generation, error_code=code,
                affected_assignment_ids=assignment_ids, failure_details=details,
            )
            return _IdentityInvalidationOutcome(False, campaign["identity_generation"])
        except RevisionConflictError:
            latest = self._campaign(campaign_id)
            return _IdentityInvalidationOutcome(True, latest["identity_generation"])

    def _invalidate_identity_or_stale(self, campaign_id: str, revision: int,
                                      generation: int, *, error_code: str,
                                      affected_assignment_ids: list[str]) -> _IdentityInvalidationOutcome:
        return self._invalidate_preflight(
            campaign_id, revision, generation, error_code, affected_assignment_ids,
        )

    def _identity_stop_from_error(self, campaign_id: str, revision: int, generation: int,
                                  error: CampaignValidationError,
                                  assignment_ids: list[str]) -> _IdentityGenerationStopped:
        if error.code not in RUNTIME_IDENTITY_FAILURE_CODES:
            raise error
        return _IdentityGenerationStopped(self._invalidate_identity_or_stale(
            campaign_id, revision, generation, error_code=error.code,
            affected_assignment_ids=assignment_ids,
        ))

    @staticmethod
    def _preflight_from_invalidation(outcome: _IdentityInvalidationOutcome) -> PreflightResult:
        return PreflightResult(outcome.stale, False, outcome.identity_generation)

    async def prepare_batch(self, campaign_id: str, assignment_ids: Sequence[str], identity_generation: int) -> BatchResult:
        if type(identity_generation) is not int or identity_generation < 0:
            raise ValueError("identity_generation must be a non-negative integer")
        campaign = self._campaign(campaign_id)
        if campaign.get("status") != "running" or campaign.get("identity_generation") != identity_generation:
            return BatchResult((), tuple(assignment_ids), True)
        if not assignment_ids or len(assignment_ids) > int(campaign.get("batch_size", 3)):
            raise CampaignValidationError("allocation_unsatisfied")
        assignments = sorted((self._assignment(campaign_id, item) for item in assignment_ids), key=lambda row: (row["position"], row["assignment_id"]))
        for row in assignments:
            if row.get("parent_assignment_id") and self.store.verified_parent_receipt(campaign_id, row["assignment_id"]) is None:
                raise CampaignValidationError("parent_comment_not_found")
        campaign = self._campaign(campaign_id)
        if campaign.get("status") != "running" or campaign.get("identity_generation") != identity_generation:
            return BatchResult((), tuple(assignment_ids), True)
        campaign_snapshot_revision = campaign["revision"]
        try:
            bindings = await self.gateway.open_many([row["profile_ref"] for row in assignments], campaign_id=campaign_id)
        except CampaignValidationError as error:
            stopped = self._identity_stop_from_error(
                campaign_id, campaign_snapshot_revision, identity_generation, error,
                [row["assignment_id"] for row in assignments],
            )
            return BatchResult(
                (), tuple(row["assignment_id"] for row in assignments),
                error.code != "profile_close_failed",
            )
        prepared: list[str] = []
        failed: list[str] = []
        runtime_failure: CampaignValidationError | None = None
        heartbeat = _LeaseHeartbeat(lambda: self.gateway.refresh_leases([row["profile_ref"] for row in assignments], campaign_id=campaign_id))
        await heartbeat.__aenter__()
        try:
            if self._campaign(campaign_id).get("status") != "running":
                raise CampaignValidationError("invalid_state_transition")
            await self._verify_prepare_batch_identities(
                campaign, assignments, bindings, campaign_snapshot_revision, identity_generation,
            )
            current_campaign = self._campaign(campaign_id)
            if (current_campaign.get("status") != "running"
                    or current_campaign.get("identity_generation") != identity_generation):
                raise _IdentityGenerationStopped(_IdentityInvalidationOutcome(
                    True, current_campaign["identity_generation"],
                ))
            prepare_context = {**campaign, "_identity_snapshot_revision": campaign_snapshot_revision,
                               "_identity_generation": identity_generation}
            outcomes = await asyncio.gather(*(
                self._prepare_one(prepare_context, row, binding)
                for row, binding in zip(assignments, bindings, strict=True)
            ), return_exceptions=True)
            stopped = next((value for value in outcomes if isinstance(value, _IdentityGenerationStopped)), None)
            runtime = next((value for value in outcomes if isinstance(value, CampaignValidationError)
                            and value.code in RUNTIME_IDENTITY_FAILURE_CODES), None)
            if stopped is not None:
                failed.extend(row["assignment_id"] for row in assignments)
            elif runtime is not None:
                runtime_failure = runtime
                failed.extend(row["assignment_id"] for row in assignments)
            else:
                for row, outcome in zip(assignments, outcomes, strict=True):
                    if isinstance(outcome, Exception):
                        failed.append(row["assignment_id"])
                        self._fail_prepare(row, outcome)
                    else:
                        prepared.append(row["assignment_id"])
        except _IdentityGenerationStopped:
            failed.extend(row["assignment_id"] for row in assignments)
        except CampaignValidationError as error:
            if error.code not in RUNTIME_IDENTITY_FAILURE_CODES:
                raise
            runtime_failure = error
            failed.extend(row["assignment_id"] for row in assignments)
        finally:
            closed = await self.gateway.close_bindings(bindings)
            await heartbeat.__aexit__(None, None, None)
            await self.gateway.release_campaign_lease(campaign_id)
        # A failed close is the final ownership fact about this window.  It wins
        # over an earlier lease/heartbeat failure and uses the authority captured
        # before opening; a CAS loser therefore cannot touch a newer generation.
        if not all(closed.values()):
            for row, binding in zip(assignments, bindings, strict=True):
                if not closed.get(str(binding.profile_id), False):
                    self._quarantine_profile(row["profile_ref"])
            runtime_failure = CampaignValidationError("profile_close_failed")
        if heartbeat.lost and runtime_failure is None:
            runtime_failure = CampaignValidationError("redis_unavailable")
        if runtime_failure is not None:
            self._identity_stop_from_error(
                campaign_id, campaign_snapshot_revision, identity_generation,
                runtime_failure,
                list(getattr(runtime_failure, "assignment_ids", ()))
                or [row["assignment_id"] for row in assignments],
            )
            failed = [row["assignment_id"] for row in assignments]
            return BatchResult((), tuple(failed), all(closed.values()))
        # Only a fully confirmed close may start a future batch.  The coordinator
        # decides which eligible IDs to enqueue and must use a new generation ID.
        if self._queue is not None:
            enqueue = getattr(self._queue, "enqueue_prepare_generation", None)
            if callable(enqueue):
                await self._enqueue_next_prepare_generation(campaign_id)
        return BatchResult(tuple(prepared), tuple(failed), True)

    async def submit_assignment(self, campaign_id: str, assignment_id: str, approved_revision: int) -> dict:
        campaign, assignment = self._campaign(campaign_id), self._assignment(campaign_id, assignment_id)
        identity_generation = campaign.get("identity_generation")
        if assignment["revision"] != approved_revision or assignment["status"] != "awaiting_step_approval":
            return {"stale": True, "submitted": False, "identity_generation": identity_generation}
        approval = self.store.get_approval(assignment_id, approved_revision)
        if not approval or approval.get("consumed_at"):
            raise CampaignValidationError("approval_revision_mismatch")
        preflight = (assignment.get("evidence") or {}).get("account_preflight")
        if (
            campaign.get("status") != "running" or type(identity_generation) is not int or identity_generation < 1
            or assignment.get("identity_generation") != identity_generation
            or not isinstance(preflight, dict) or preflight.get("identity_generation") != identity_generation
        ):
            return {"stale": True, "submitted": False, "identity_generation": identity_generation}
        if assignment.get("parent_assignment_id") and self.store.verified_parent_receipt(campaign_id, assignment_id) is None:
            raise CampaignValidationError("parent_comment_not_found")
        try:
            leases = await self._acquire_video_lease(campaign["video_id"])
        except CampaignValidationError as error:
            if error.code not in RUNTIME_IDENTITY_FAILURE_CODES:
                raise
            stopped = self._identity_stop_from_error(
                campaign_id, campaign["revision"], identity_generation, error, [assignment_id],
            )
            return {"stale": stopped.outcome.stale, "submitted": False,
                    "identity_generation": stopped.outcome.identity_generation}
        binding = None
        clicked = False
        runtime_account_key = assignment["expected_username"]
        result_payload: dict | None = None
        runtime_failure: CampaignValidationError | None = None
        close_failed = False
        try:
            binding = await self.gateway.open_one(assignment["profile_ref"], campaign_id=campaign_id)
            heartbeat = _LeaseHeartbeat(lambda: self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases))
            await heartbeat.__aenter__()
            try:
                runtime_account_key = await self._runtime_identity_or_stop(
                    campaign, assignment, binding.page, campaign["revision"], identity_generation,
                )
                page_evidence = await self._prepare_page(
                    campaign, assignment, binding.page, retries=3,
                    campaign_snapshot_revision=campaign["revision"], identity_generation=identity_generation,
                )
            except (_IdentityGenerationStopped, RevisionConflictError):
                raise
            except CampaignValidationError as error:
                if error.code in RUNTIME_IDENTITY_FAILURE_CODES:
                    raise
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise
            except Exception:
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise
            evidence = assignment.get("evidence") if isinstance(assignment.get("evidence"), dict) else {}
            expected_evidence = self._approval_evidence(evidence)
            actual_evidence = self._preparation_evidence(campaign, assignment, {key: value for key, value in page_evidence.items() if not key.startswith("_")})
            actual_evidence["account_preflight"] = evidence.get("account_preflight")
            if expected_evidence != self._approval_evidence(actual_evidence):
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise CampaignValidationError("approval_revision_mismatch")
            if heartbeat.lost or not await self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases):
                raise CampaignValidationError("redis_unavailable")
            # Refresh/verification happens while status remains awaiting approval.
            current = self._assignment(campaign_id, assignment_id)
            if current["revision"] != approved_revision or current["status"] != "awaiting_step_approval":
                raise CampaignValidationError("approval_revision_mismatch")
            submit = page_evidence.get("_submit") or await self._submit_locator(campaign, binding.page)
            if heartbeat.lost or not await self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases):
                raise CampaignValidationError("redis_unavailable")
            baseline = await self._candidates(binding.page, campaign, assignment)
            if heartbeat.lost or not await self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases):
                raise CampaignValidationError("redis_unavailable")
            submitting = self.store.begin_submitting(
                campaign_id, assignment_id, approved_revision, identity_generation,
            )
            clicked = True
            await getattr(submit, "handle", submit).click()
            verifying = self.store.update_assignment_status(assignment_id, submitting["revision"], "verifying_receipt")
            receipt = build_receipt(video_id=campaign["video_id"], profile_ref=assignment["profile_ref"], expected_username=runtime_account_key, text=assignment["resolved_text"], screenshot_path=await self._screenshot(binding.page), **(page_evidence.get("parent") or {}))
            verified, evidence = await self._verify_post_click(campaign, assignment, receipt, baseline, binding.page)
            target = "published_verified" if verified else "published_unverified"
            result = self.store.save_receipt_and_transition(
                assignment_id, verifying["revision"],
                {**receipt, **evidence, "status": target}, target,
                error_code="" if verified else "comment_receipt_unverified",
                pause_descendants_error_code="" if verified else "comment_receipt_unverified",
            )
            if verified:
                self.store.resume_verified_children(campaign_id, assignment_id)
            result_payload = result
        except Exception as exc:
            if clicked:
                current = self.store.get_assignment(assignment_id)
                if current and current["status"] in {"submitting", "verifying_receipt"}:
                    receipt = build_receipt(video_id=campaign["video_id"], profile_ref=assignment["profile_ref"], expected_username=runtime_account_key, text=assignment["resolved_text"], screenshot_path=None)
                    result_payload = self.store.save_receipt_and_transition(
                        assignment_id, current["revision"], {**receipt, "status": "published_unverified"},
                        "published_unverified", error_code="comment_submit_uncertain",
                        pause_descendants_error_code="comment_submit_uncertain",
                    )
                else:
                    raise CampaignValidationError("comment_submit_uncertain") from exc
            elif isinstance(exc, _IdentityGenerationStopped):
                result_payload = {"stale": exc.outcome.stale, "submitted": False, "identity_generation": exc.outcome.identity_generation}
            elif isinstance(exc, RevisionConflictError):
                current = self._campaign(campaign_id)
                result_payload = {"stale": True, "submitted": False, "identity_generation": current["identity_generation"]}
            elif isinstance(exc, CampaignValidationError) and exc.code in RUNTIME_IDENTITY_FAILURE_CODES:
                runtime_failure = exc
            else:
                # The approval was consumed but no click occurred.  A fresh revision
                # invalidates it; a later resume must prepare and approve anew.
                current = self.store.get_assignment(assignment_id)
                if current and current["status"] == "awaiting_step_approval" and current["revision"] == approved_revision:
                    self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                if isinstance(exc, CampaignValidationError):
                    raise
                raise CampaignValidationError("comment_submit_uncertain") from exc
        finally:
            if binding is not None:
                closed = await self.gateway.close_bindings([binding])
                if not all(closed.values()):
                    self._quarantine_profile(assignment["profile_ref"])
                    close_failed = True
            if "heartbeat" in locals():
                await heartbeat.__aexit__(None, None, None)
            await self._release_all(leases)
            await self.gateway.release_campaign_lease(campaign_id)
        affected_ids = [assignment_id]
        if close_failed:
            rows = self.store.list_assignments(campaign_id)
            terminal = {"published_verified", "published_unverified", "failed", "cancelled"}
            active_ids = [row["assignment_id"] for row in rows if row.get("status") not in terminal]
            if active_ids:
                affected_ids = active_ids
                runtime_failure = CampaignValidationError("profile_close_failed")
            elif result_payload is not None:
                # The comment outcome is already durable.  There is no active
                # generation left to invalidate, so retain the honest Receipt and
                # quarantine record rather than fabricating a failed generation.
                # Keep ``close_failed`` true: it also suppresses the next batch.
                pass
        if runtime_failure is not None:
            stopped = self._identity_stop_from_error(
                campaign_id, campaign["revision"], identity_generation, runtime_failure, affected_ids,
            )
            if result_payload is None:
                return {"stale": stopped.outcome.stale, "submitted": False,
                        "identity_generation": stopped.outcome.identity_generation}
        if result_payload is not None:
            if (result_payload.get("status") == "published_verified" and self._queue is not None
                    and not close_failed):
                enqueue = getattr(self._queue, "enqueue_prepare_generation", None)
                if callable(enqueue):
                    await self._enqueue_next_prepare_generation(campaign_id)
            return result_payload
        raise CampaignValidationError("comment_submit_uncertain")

    async def _runtime_identity_or_stop(self, campaign: dict, assignment: dict, page: Any,
                                        campaign_revision: int, identity_generation: int) -> str:
        try:
            await page.goto(campaign["canonical_url"], wait_until="domcontentloaded")
            await verify_video(page, campaign["video_id"])
            actual = await read_tiktok_identity(
                page, self._definition(self._settings(campaign), "account", "click"), resolver=self.resolver,
            )
        except CampaignValidationError:
            # The public prepare/submit boundary owns the one generation CAS.
            # Keeping this phase read-only lets a subsequent failed close take
            # precedence without a second invalidation attempt.
            raise
        preflight = (assignment.get("evidence") or {}).get("account_preflight")
        if not isinstance(preflight, dict) or actual.account_key != preflight.get("account_key"):
            raise CampaignValidationError("tiktok_identity_changed")
        return actual.account_key

    async def _prepare_one(self, campaign: dict, assignment: dict, binding: Any) -> None:
        opening = self.store.update_assignment_status(assignment["assignment_id"], assignment["revision"], "opening_profile")
        page_evidence = await self._prepare_page(campaign, opening, binding.page, retries=3,
                                                 campaign_snapshot_revision=campaign.get("_identity_snapshot_revision"),
                                                 identity_generation=campaign.get("_identity_generation"))
        if not await self.gateway.refresh_leases([assignment["profile_ref"]], campaign_id=campaign["id"]):
            raise CampaignValidationError("redis_unavailable")
        evidence = self._preparation_evidence(campaign, assignment, {key: value for key, value in page_evidence.items() if not key.startswith("_")})
        preflight = (assignment.get("evidence") or {}).get("account_preflight")
        if isinstance(preflight, dict):
            evidence["account_preflight"] = preflight
        evidence["screenshot_path"] = await self._screenshot(binding.page)
        if self._campaign(campaign["id"]).get("status") != "running":
            raise CampaignValidationError("invalid_state_transition")
        self.store.update_assignment_status(
            assignment["assignment_id"], page_evidence["_input_revision"],
            "awaiting_step_approval", evidence=evidence,
        )

    async def _verify_prepare_batch_identities(self, campaign: dict, assignments: Sequence[dict],
                                               bindings: Sequence[Any], campaign_revision: int,
                                               identity_generation: int) -> None:
        account_definition = self._definition(self._settings(campaign), "account", "click")
        for row, binding in zip(assignments, bindings, strict=True):
            preflight = (row.get("evidence") or {}).get("account_preflight")
            try:
                await binding.page.goto(campaign["canonical_url"], wait_until="domcontentloaded")
                await verify_video(binding.page, campaign["video_id"])
                actual = await read_tiktok_identity(binding.page, account_definition, resolver=self.resolver)
            except CampaignValidationError as error:
                error.assignment_ids = [row["assignment_id"]]
                raise
            if not isinstance(preflight, dict) or actual.account_key != preflight.get("account_key"):
                error = CampaignValidationError("tiktok_identity_changed")
                error.assignment_ids = [row["assignment_id"]]
                raise error

    async def _prepare_page(self, campaign: dict, assignment: dict, page: Any, *, retries: int,
                            campaign_snapshot_revision: int | None = None,
                            identity_generation: int | None = None) -> dict[str, Any]:
        settings = self._settings(campaign)
        last: Exception | None = None
        for index in range(retries):
            try:
                current = self._assignment(campaign["id"], assignment["assignment_id"])
                if current["status"] == "opening_profile":
                    self.store.update_assignment_status(assignment["assignment_id"], current["revision"], "locating_video")
                await page.goto(campaign["canonical_url"], wait_until="domcontentloaded")
                video_evidence = await verify_video(page, campaign["video_id"])
                account_evidence = await verify_logged_in_username(page, assignment["expected_username"], self._definition(settings, "account", "click"), resolver=self.resolver)
                await open_comment_panel(page, self._definition(settings, "entry", "click"), 10_000, resolver=self.resolver)
                current = self._assignment(campaign["id"], assignment["assignment_id"])
                if current["status"] == "locating_video" and assignment.get("parent_assignment_id"):
                    self.store.update_assignment_status(assignment["assignment_id"], current["revision"], "locating_parent")
                    current = self._assignment(campaign["id"], assignment["assignment_id"])
                if current["status"] == "locating_video":
                    self.store.update_assignment_status(assignment["assignment_id"], current["revision"], "preparing_comment")
                parent_evidence: dict[str, Any] = {}
                scoped_submit = None
                if assignment.get("parent_assignment_id"):
                    receipt = self.store.verified_parent_receipt(campaign["id"], assignment["assignment_id"])
                    if receipt is None:
                        raise CampaignValidationError("parent_comment_not_found")
                    parent = await locate_parent_comment(page, receipt)
                    scope = await open_scoped_reply(parent, str(receipt.get("expected_username") or ""))
                    locator, scoped_submit = scope["input"], scope["submit"]
                    parent_evidence = {
                        "parent_receipt_id": receipt["receipt_id"],
                        **dict(scope.get("parent_scope") or {}),
                        "parent_author": scope.get("parent_author", ""),
                    }
                    current = self._assignment(campaign["id"], assignment["assignment_id"])
                    if current["status"] == "locating_parent":
                        self.store.update_assignment_status(assignment["assignment_id"], current["revision"], "preparing_comment")
                else:
                    locator = await locate_comment_input(page, self._definition(settings, "input", "input"), resolver=self.resolver)
                handle = getattr(locator, "handle", locator)
                if campaign_snapshot_revision is not None and identity_generation is not None:
                    try:
                        current = self.store.begin_comment_input(
                            campaign["id"], assignment["assignment_id"], current["revision"], identity_generation,
                        )
                    except RevisionConflictError:
                        raise _IdentityGenerationStopped(_IdentityInvalidationOutcome(
                            True, self._campaign(campaign["id"])["identity_generation"],
                        )) from None
                await handle.focus()
                await handle.evaluate("element => { if ('value' in element) element.value=''; else element.textContent=''; element.dispatchEvent(new InputEvent('input', {bubbles:true,inputType:'deleteContentBackward'})); }")
                await human_type(page, assignment["resolved_text"], timing={"source": "builtin", "interval_ms": [35, 75]})
                if await _read_input_value(handle) != assignment["resolved_text"]:
                    raise CampaignValidationError("comment_input_not_found")
                return {"video": video_evidence, "account": account_evidence, "parent": parent_evidence,
                        "_submit": scoped_submit, "_input_revision": current["revision"]}
            except _IdentityGenerationStopped:
                raise
            except CampaignValidationError as exc:
                if exc.code not in {"comment_panel_not_ready", "comment_input_not_found"} or index + 1 == retries:
                    raise
                last = exc
            except Exception as exc:
                if index + 1 == retries:
                    raise CampaignValidationError("comment_panel_not_ready") from exc
                last = exc
        raise CampaignValidationError("comment_panel_not_ready") from last

    async def _submit_locator(self, campaign: dict, page: Any) -> Any:
        return await locate_submit_control(page, self._definition(self._settings(campaign), "submit", "click"), resolver=self.resolver)

    async def _verify_post_click(self, campaign: dict, assignment: dict, receipt: dict, baseline: list[dict], page: Any) -> tuple[bool, dict]:
        """No saved, explicit receipt locator means an honest unverified result."""
        try:
            await page.goto(campaign["canonical_url"], wait_until="domcontentloaded")
            await verify_video(page, campaign["video_id"])
            candidate = verify_receipt_candidates(before=baseline, after=await self._candidates(page, campaign, assignment), receipt=receipt)
            if candidate is None:
                return False, {}
            return True, {key: candidate[key] for key in ("platform_comment_id", "comment_permalink", "stable_attributes", "author_profile_href") if key in candidate}
        except Exception:
            return False, {}

    async def _candidates(self, page: Any, campaign: dict, assignment: dict) -> list[dict]:
        if self._receipt_candidates is None:
            return []
        value = self._receipt_candidates(page, campaign, assignment)
        if inspect.isawaitable(value): value = await value
        return [dict(item) for item in value] if isinstance(value, list) and all(isinstance(item, Mapping) for item in value) else []

    def _settings(self, campaign: dict) -> Mapping[str, Any]:
        return self._settings_provider(campaign) if self._settings_provider else campaign.get("element_bindings", {})

    def _definition(self, settings: Mapping[str, Any], name: str, kind: str) -> dict:
        identifier = settings.get(f"{name}_element_id")
        if not isinstance(identifier, str) or not self._element_provider:
            raise CampaignValidationError("comment_panel_not_ready")
        element = self._element_provider(identifier)
        if not isinstance(element, Mapping) or element.get("status") != "active" or element.get("kind") != kind or not isinstance(element.get("definition"), dict):
            raise CampaignValidationError("comment_panel_not_ready")
        return dict(element["definition"])

    def _preparation_evidence(self, campaign: dict, assignment: dict, page_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        settings = self._settings(campaign)
        elements: dict[str, dict[str, Any]] = {}
        for name in ("entry", "input", "submit", "account"):
            identifier = settings.get(f"{name}_element_id")
            element = self._element_provider(identifier) if self._element_provider and isinstance(identifier, str) else None
            if isinstance(element, Mapping):
                definition = element.get("definition")
                encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() if isinstance(definition, dict) else b""
                elements[name] = {"id": identifier, "revision": element.get("revision"), "definition_sha256": hashlib.sha256(encoded).hexdigest()}
        page = dict(page_evidence or {})
        parent = page.pop("parent", {})
        return {
            "video_id": campaign["video_id"], "expected_username": assignment["expected_username"],
            "resolved_text_sha256": hashlib.sha256(assignment["resolved_text"].encode()).hexdigest(),
            "element_bindings": elements,
            "page_evidence": page,
            "parent_evidence": parent if isinstance(parent, Mapping) else {},
            "input_evidence": {"text_sha256": hashlib.sha256(assignment["resolved_text"].encode()).hexdigest()},
        }

    @staticmethod
    def _approval_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Stable approval facts only; screenshots and observed-at data are volatile."""
        return {
            "video_id": evidence.get("video_id"),
            "expected_username": evidence.get("expected_username"),
            "resolved_text_sha256": evidence.get("resolved_text_sha256"),
            "element_bindings": evidence.get("element_bindings"),
            "account_preflight": evidence.get("account_preflight"),
            "parent": evidence.get("parent_evidence"),
        }

    async def _screenshot(self, page: Any) -> str:
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self._evidence_dir / evidence_filename()
        await page.screenshot(path=str(path))
        return f"evidence/{path.name}"

    def _campaign(self, campaign_id: str) -> dict:
        value = self.store.get_campaign(campaign_id)
        if value is None:
            raise CampaignNotFoundError(campaign_id)
        return value

    def _assignment(self, campaign_id: str, assignment_id: str) -> dict:
        value = self.store.get_assignment(assignment_id)
        if value is None or value.get("campaign_id") != campaign_id:
            raise CampaignNotFoundError(assignment_id)
        return value

    def _fail_prepare(self, assignment: dict, error: Exception) -> None:
        current = self.store.get_assignment(assignment["assignment_id"])
        if current and current["status"] not in {"failed", "paused"}:
            campaign = self.store.get_campaign(assignment["campaign_id"])
            target = "paused" if campaign and campaign.get("status") == "paused" else "failed"
            self.store.fail_assignment_and_pause_descendants(
                assignment["assignment_id"], current["revision"], target,
                str(getattr(error, "code", "comment_panel_not_ready")),
            )

    def _pause_for_close(self, campaign_id: str, revision: int | None = None,
                         identity_generation: int | None = None,
                         assignment_ids: Sequence[str] = ()) -> None:
        """Close failures are generation failures, never an unguarded pause."""
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            return
        snapshot_revision = campaign["revision"] if revision is None else revision
        generation = campaign.get("identity_generation") if identity_generation is None else identity_generation
        if type(generation) is not int or generation < 1:
            return
        ids = list(assignment_ids)
        if not ids:
            return
        try:
            self._identity_stop_from_error(
                campaign_id, snapshot_revision, generation,
                CampaignValidationError("profile_close_failed"), ids,
            )
        except _IdentityGenerationStopped:
            pass

    def _quarantine_profile(self, profile_ref: str) -> None:
        metadata = self.store.get_profile_metadata(profile_ref)
        if metadata is None:
            return
        self.store.upsert_profile_metadata(**{**metadata, "enabled": False, "health_status": "unhealthy"})

    async def _acquire_video_lease(self, video_id: str) -> list[Any]:
        if self._lease_factory is None:
            return []
        leases = [self._lease_factory(f"video_submit:{video_id}")]
        for lease in leases:
            value = lease.acquire()
            if inspect.isawaitable(value): value = await value
            if not value:
                await self._release_all(leases)
                raise CampaignValidationError("redis_unavailable")
        return leases

    async def _refresh_all(self, leases: Sequence[Any]) -> bool:
        for lease in leases:
            value = lease.refresh()
            if inspect.isawaitable(value): value = await value
            if not value: return False
        return True

    async def _refresh_submit_leases(self, campaign_id: str, profile_ref: str, leases: Sequence[Any]) -> bool:
        return await self.gateway.refresh_leases([profile_ref], campaign_id=campaign_id) and await self._refresh_all(leases)

    async def _release_all(self, leases: Sequence[Any]) -> None:
        for lease in reversed(leases):
            try:
                value = lease.release()
                if inspect.isawaitable(value): await value
            except Exception:
                pass

    async def _enqueue_next_prepare_generation(self, campaign_id: str) -> None:
        generation = self.store.next_prepare_generation(campaign_id)
        campaign = self._campaign(campaign_id)
        identity_generation = campaign.get("identity_generation")
        if type(identity_generation) is not int or identity_generation < 0:
            return
        value = self._queue.enqueue_prepare_generation(
            campaign_id, generation, identity_generation
        )
        if inspect.isawaitable(value):
            await value
        marker = getattr(self.store, "mark_reconcile_prepare_generation", None)
        if callable(marker):
            marker(campaign_id, generation)
