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

from .errors import CampaignNotFoundError, CampaignValidationError, RevisionConflictError
from .locator import (locate_comment_input, locate_parent_comment, locate_submit_control, open_scoped_reply, open_comment_panel,
                      verify_logged_in_username, verify_video)
from .receipts import build_receipt, evidence_filename, normalize_comment_text, verify_receipt_candidates


@dataclass(frozen=True, slots=True)
class BatchResult:
    prepared: tuple[str, ...]
    failed: tuple[str, ...] = ()
    close_confirmed: bool = True


class _LeaseHeartbeat:
    def __init__(self, refresh: Callable[[], Any], *, interval_seconds: float = 20) -> None:
        self._refresh, self._interval, self.lost = refresh, interval_seconds, False
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
            except Exception:
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

    async def prepare_batch(self, campaign_id: str, assignment_ids: Sequence[str]) -> BatchResult:
        campaign = self._campaign(campaign_id)
        if not assignment_ids or len(assignment_ids) > int(campaign.get("batch_size", 3)):
            raise CampaignValidationError("allocation_unsatisfied")
        assignments = sorted((self._assignment(campaign_id, item) for item in assignment_ids), key=lambda row: (row["position"], row["assignment_id"]))
        for row in assignments:
            if row.get("parent_assignment_id") and self.store.verified_parent_receipt(campaign_id, row["assignment_id"]) is None:
                raise CampaignValidationError("parent_comment_not_found")
        bindings = await self.gateway.open_many([row["profile_ref"] for row in assignments], campaign_id=campaign_id)
        prepared: list[str] = []
        failed: list[str] = []
        heartbeat = _LeaseHeartbeat(lambda: self.gateway.refresh_leases([row["profile_ref"] for row in assignments], campaign_id=campaign_id))
        await heartbeat.__aenter__()
        try:
            if self._campaign(campaign_id).get("status") != "running":
                raise CampaignValidationError("invalid_state_transition")
            outcomes = await asyncio.gather(*(self._prepare_one(campaign, row, binding) for row, binding in zip(assignments, bindings, strict=True)), return_exceptions=True)
            for row, outcome in zip(assignments, outcomes, strict=True):
                if isinstance(outcome, Exception):
                    failed.append(row["assignment_id"])
                    self._fail_prepare(row, outcome)
                else:
                    prepared.append(row["assignment_id"])
        finally:
            closed = await self.gateway.close_bindings(bindings)
            await heartbeat.__aexit__(None, None, None)
            await self.gateway.release_campaign_lease(campaign_id)
        if not all(closed.values()):
            for row, binding in zip(assignments, bindings, strict=True):
                if not closed.get(str(binding.profile_id), False):
                    self._quarantine_profile(row["profile_ref"])
            current = self._campaign(campaign_id)
            self.store.transition_campaign_status(campaign_id, current["revision"], "paused", pause_reason="profile_close_failed")
            return BatchResult(tuple(prepared), tuple(failed), False)
        if heartbeat.lost:
            raise CampaignValidationError("redis_unavailable")
        # Only a fully confirmed close may start a future batch.  The coordinator
        # decides which eligible IDs to enqueue and must use a new generation ID.
        if self._queue is not None:
            enqueue = getattr(self._queue, "enqueue_prepare_generation", None)
            if callable(enqueue):
                await self._enqueue_next_prepare_generation(campaign_id)
        return BatchResult(tuple(prepared), tuple(failed), True)

    async def submit_assignment(self, campaign_id: str, assignment_id: str, approved_revision: int) -> dict:
        campaign, assignment = self._campaign(campaign_id), self._assignment(campaign_id, assignment_id)
        if campaign.get("status") != "running" or assignment["revision"] != approved_revision or assignment["status"] != "awaiting_step_approval":
            raise CampaignValidationError("approval_revision_mismatch")
        approval = self.store.get_approval(assignment_id, approved_revision)
        if not approval or approval.get("consumed_at"):
            raise CampaignValidationError("approval_revision_mismatch")
        if assignment.get("parent_assignment_id") and self.store.verified_parent_receipt(campaign_id, assignment_id) is None:
            raise CampaignValidationError("parent_comment_not_found")
        leases = await self._acquire_video_lease(campaign["video_id"])
        binding = None
        clicked = False
        try:
            # This is a Store transaction, so competing jobs cannot consume it.
            self.store.consume_submit_approval(campaign_id, assignment_id, approved_revision)
            binding = await self.gateway.open_one(assignment["profile_ref"], campaign_id=campaign_id)
            heartbeat = _LeaseHeartbeat(lambda: self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases))
            await heartbeat.__aenter__()
            try:
                page_evidence = await self._prepare_page(campaign, assignment, binding.page, retries=3)
            except Exception:
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise
            evidence = assignment.get("evidence") if isinstance(assignment.get("evidence"), dict) else {}
            expected_evidence = {key: value for key, value in evidence.items() if key != "screenshot_path"}
            if expected_evidence != self._preparation_evidence(campaign, assignment, {key: value for key, value in page_evidence.items() if not key.startswith("_")}):
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise CampaignValidationError("approval_revision_mismatch")
            if heartbeat.lost or not await self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases):
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise CampaignValidationError("redis_unavailable")
            # Refresh/verification happens while status remains awaiting approval.
            current = self._assignment(campaign_id, assignment_id)
            if current["revision"] != approved_revision or current["status"] != "awaiting_step_approval":
                raise CampaignValidationError("approval_revision_mismatch")
            submit = page_evidence.get("_submit") or await self._submit_locator(campaign, binding.page)
            if heartbeat.lost or not await self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases):
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise CampaignValidationError("redis_unavailable")
            baseline = await self._candidates(binding.page, campaign, assignment)
            if heartbeat.lost or not await self._refresh_submit_leases(campaign_id, assignment["profile_ref"], leases):
                self.store.invalidate_submit_approval(campaign_id, assignment_id, approved_revision)
                raise CampaignValidationError("redis_unavailable")
            submitting = self.store.begin_submitting(campaign_id, assignment_id, approved_revision)
            clicked = True
            await getattr(submit, "handle", submit).click()
            verifying = self.store.update_assignment_status(assignment_id, submitting["revision"], "verifying_receipt")
            receipt = build_receipt(video_id=campaign["video_id"], profile_ref=assignment["profile_ref"], expected_username=assignment["expected_username"], text=assignment["resolved_text"], screenshot_path=await self._screenshot(binding.page), **(page_evidence.get("parent") or {}))
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
            if verified and self._queue is not None:
                enqueue = getattr(self._queue, "enqueue_prepare_generation", None)
                if callable(enqueue):
                    await self._enqueue_next_prepare_generation(campaign_id)
            return result
        except Exception as exc:
            if clicked:
                current = self.store.get_assignment(assignment_id)
                if current and current["status"] in {"submitting", "verifying_receipt"}:
                    receipt = build_receipt(video_id=campaign["video_id"], profile_ref=assignment["profile_ref"], expected_username=assignment["expected_username"], text=assignment["resolved_text"], screenshot_path=None)
                    result = self.store.save_receipt_and_transition(
                        assignment_id, current["revision"],
                        {**receipt, "status": "published_unverified"},
                        "published_unverified", error_code="comment_submit_uncertain",
                        pause_descendants_error_code="comment_submit_uncertain",
                    )
                    return result
                raise CampaignValidationError("comment_submit_uncertain") from exc
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
                    self._pause_for_close(campaign_id)
            if "heartbeat" in locals():
                await heartbeat.__aexit__(None, None, None)
            await self._release_all(leases)
            await self.gateway.release_campaign_lease(campaign_id)

    async def _prepare_one(self, campaign: dict, assignment: dict, binding: Any) -> None:
        opening = self.store.update_assignment_status(assignment["assignment_id"], assignment["revision"], "opening_profile")
        page_evidence = await self._prepare_page(campaign, opening, binding.page, retries=3)
        if not await self.gateway.refresh_leases([assignment["profile_ref"]], campaign_id=campaign["id"]):
            raise CampaignValidationError("redis_unavailable")
        evidence = self._preparation_evidence(campaign, assignment, {key: value for key, value in page_evidence.items() if not key.startswith("_")})
        evidence["screenshot_path"] = await self._screenshot(binding.page)
        if self._campaign(campaign["id"]).get("status") != "running":
            raise CampaignValidationError("invalid_state_transition")
        current = self._assignment(campaign["id"], assignment["assignment_id"])
        self.store.update_assignment_status(assignment["assignment_id"], current["revision"], "awaiting_step_approval", evidence=evidence)

    async def _prepare_page(self, campaign: dict, assignment: dict, page: Any, *, retries: int) -> dict[str, Any]:
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
                await handle.focus()
                await handle.evaluate("element => { if ('value' in element) element.value=''; else element.textContent=''; element.dispatchEvent(new InputEvent('input', {bubbles:true,inputType:'deleteContentBackward'})); }")
                await human_type(page, assignment["resolved_text"], timing={"source": "builtin", "interval_ms": [35, 75]})
                if await _read_input_value(handle) != assignment["resolved_text"]:
                    raise CampaignValidationError("comment_input_not_found")
                return {"video": video_evidence, "account": account_evidence, "parent": parent_evidence, "_submit": scoped_submit}
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
        return {
            "video_id": campaign["video_id"], "expected_username": assignment["expected_username"],
            "resolved_text_sha256": hashlib.sha256(assignment["resolved_text"].encode()).hexdigest(),
            "element_bindings": elements,
            "page_evidence": dict(page_evidence or {}),
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

    def _pause_for_close(self, campaign_id: str) -> None:
        campaign = self.store.get_campaign(campaign_id)
        if campaign and campaign["status"] in {"queued", "running"}:
            self.store.transition_campaign_status(campaign_id, campaign["revision"], "paused", pause_reason="profile_close_failed")

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
        value = self._queue.enqueue_prepare_generation(campaign_id, generation)
        if inspect.isawaitable(value):
            await value
        marker = getattr(self.store, "mark_reconcile_prepare_generation", None)
        if callable(marker):
            marker(campaign_id, generation)
