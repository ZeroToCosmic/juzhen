"""The only Campaign boundary allowed to handle raw AdsPower profile IDs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from .errors import CampaignValidationError


class IdentityPreflightStale(RuntimeError):
    """A Campaign identity scan lost its lease or generation without writing."""


class ProfileGateway:
    """Resolve redacted refs, bind one browser per Profile, and prove closure."""

    def __init__(
        self, store: Any, adspower: Any, sessions: Any, *,
        profile_discovery: Callable[[], Sequence[dict[str, Any]]] | None = None,
        tiler: Callable[[Sequence[Any]], Any] | None = None,
        lease_factory: Callable[[str], Any] | None = None,
        close_attempts: int = 3,
        lease_heartbeat_seconds: float = 20,
    ) -> None:
        self._store, self._adspower, self._sessions = store, adspower, sessions
        self._profile_discovery = profile_discovery
        self._tiler, self._lease_factory = tiler, lease_factory
        self._close_attempts = close_attempts
        self._lease_heartbeat_seconds = float(lease_heartbeat_seconds)
        self._leases: dict[str, Any] = {}
        self._raw_to_ref: dict[str, str] = {}
        self._confirmed_closed_refs: set[str] = set()

    def sync_discovered_profiles(self) -> list[dict]:
        """Synchronize only the store's explicit raw-profile whitelist."""
        rows = self._discover()
        return self._store.sync_profile_identities([
            {"id": row["id"], "name": row["name"], "status": row["status"]}
            for row in rows
        ])

    def resolve(self, profile_ref: str) -> str:
        get_metadata = getattr(self._store, "get_profile_metadata", None)
        if callable(get_metadata):
            metadata = get_metadata(profile_ref)
            if (
                not isinstance(metadata, dict)
                or metadata.get("enabled") is not True
                or metadata.get("health_status") != "healthy"
            ):
                raise CampaignValidationError("profile_start_failed")
        raw_id = self._store.get_raw_profile_id(profile_ref)
        if not isinstance(raw_id, str) or not raw_id:
            raise CampaignValidationError("profile_start_failed")
        if self._profile_discovery is not None and raw_id not in {row["id"] for row in self._discover()}:
            raise CampaignValidationError("profile_start_failed")
        return raw_id

    async def open_many(self, profile_refs: Sequence[str], *, campaign_id: str | None = None) -> list[Any]:
        refs = tuple(profile_refs)
        if not refs or len(refs) != len(set(refs)):
            raise CampaignValidationError("profile_start_failed")
        raw_ids = [self.resolve(ref) for ref in refs]
        started: list[str] = []
        bindings: list[Any] = []
        heartbeat_task: asyncio.Task[Any] | None = None
        heartbeat_lost = asyncio.Event()
        try:
            if campaign_id is not None:
                await self._acquire(f"campaign:{campaign_id}")
            # Acquire every lease before any browser is started.  Redis sees only
            # redacted refs, never the AdsPower identifiers held below.
            for ref, raw_id in zip(refs, raw_ids, strict=True):
                await self._acquire(f"profile:{ref}")
                self._raw_to_ref[raw_id] = ref
            if self._lease_factory is not None:
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_leases(
                        ([f"campaign:{campaign_id}"] if campaign_id else [])
                        + [f"profile:{ref}" for ref in refs],
                        heartbeat_lost,
                    )
                )
            for raw_id in raw_ids:
                # The Local API may start a browser and then fail its response.
                # Treat it as started before awaiting so cleanup remains safe.
                started.append(raw_id)
                endpoint = await self._adspower.start(raw_id)
                bindings.append(await self._sessions.connect(raw_id, endpoint))
                if heartbeat_lost.is_set():
                    raise CampaignValidationError("redis_unavailable")
            if self._tiler is not None:
                result = self._tiler(bindings)
                if hasattr(result, "__await__"):
                    await result
            if heartbeat_lost.is_set():
                raise CampaignValidationError("redis_unavailable")
            if campaign_id is not None:
                campaign = self._store.get_campaign(campaign_id)
                if campaign is None or campaign.get("status") != "running":
                    raise CampaignValidationError("invalid_state_transition")
            return bindings
        except CampaignValidationError:
            closed = await self.close_many(started)
            await self._release_unstarted_leases(refs, raw_ids, started, closed)
            await self._quarantine_unclosed(refs, raw_ids, closed, campaign_id)
            if campaign_id is not None:
                await self._release(f"campaign:{campaign_id}")
            if any(not confirmed for confirmed in closed.values()):
                raise CampaignValidationError("profile_close_failed") from None
            raise
        except Exception as exc:
            closed = await self.close_many(started)
            await self._release_unstarted_leases(refs, raw_ids, started, closed)
            await self._quarantine_unclosed(refs, raw_ids, closed, campaign_id)
            if campaign_id is not None:
                await self._release(f"campaign:{campaign_id}")
            if any(not confirmed for confirmed in closed.values()):
                raise CampaignValidationError("profile_close_failed") from None
            raise CampaignValidationError("profile_start_failed") from exc
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def open_one(self, profile_ref: str, *, campaign_id: str | None = None) -> Any:
        return (await self.open_many([profile_ref], campaign_id=campaign_id))[0]

    async def acquire_campaign_lease(self, campaign_id: str) -> None:
        """Acquire the outer lease once for a complete identity scan."""
        try:
            await self._acquire(f"campaign:{campaign_id}")
        except CampaignValidationError as exc:
            if exc.code == "profile_start_failed":
                raise IdentityPreflightStale() from None
            raise

    async def refresh_campaign_lease(self, campaign_id: str) -> bool:
        return await self.refresh_leases((), campaign_id=campaign_id)

    async def open_identity_batch(
        self, profile_refs: Sequence[str], campaign_id: str,
        expected_identity_generation: int,
    ) -> list[Any]:
        """Open one identity-only batch under an existing Campaign lease."""
        campaign = self._store.get_campaign(campaign_id)
        if (
            campaign is None or campaign.get("status") != "running"
            or campaign.get("identity_generation") != expected_identity_generation
        ):
            raise IdentityPreflightStale()
        if self._lease_factory is not None and f"campaign:{campaign_id}" not in self._leases:
            raise IdentityPreflightStale()
        refs = tuple(profile_refs)
        if not refs or len(refs) != len(set(refs)):
            raise CampaignValidationError("profile_start_failed")
        raw_ids = [self.resolve(ref) for ref in refs]
        started: list[str] = []
        bindings: list[Any] = []
        failure: Exception | None = None
        heartbeat_task: asyncio.Task[Any] | None = None
        heartbeat_lost = asyncio.Event()
        heartbeat_dependency_failed = [False]
        try:
            for ref, raw_id in zip(refs, raw_ids, strict=True):
                try:
                    await self._acquire(f"profile:{ref}")
                except CampaignValidationError as exc:
                    # A false lease is ordinary contention, not a profile failure.
                    if exc.code == "profile_start_failed":
                        raise IdentityPreflightStale() from None
                    raise
                self._raw_to_ref[raw_id] = ref
            # Start only after *all* profile leases are held.  It refreshes the
            # campaign and every held profile through slow start/connect/tiling.
            if self._lease_factory is not None:
                heartbeat_task = asyncio.create_task(self._heartbeat_leases(
                    [f"campaign:{campaign_id}"] + [f"profile:{ref}" for ref in refs],
                    heartbeat_lost, heartbeat_dependency_failed,
                ))
            for index, raw_id in enumerate(raw_ids):
                started.append(raw_id)
                try:
                    endpoint = await self._adspower.start(raw_id)
                except CampaignValidationError:
                    raise
                except Exception as exc:
                    raise CampaignValidationError("profile_start_failed") from exc
                if heartbeat_lost.is_set():
                    if heartbeat_dependency_failed[0]:
                        raise CampaignValidationError("redis_unavailable")
                    raise IdentityPreflightStale()
                try:
                    bindings.append(await self._sessions.connect(raw_id, endpoint))
                except CampaignValidationError:
                    raise
                except Exception as exc:
                    raise CampaignValidationError("cdp_connect_failed") from exc
                if heartbeat_lost.is_set():
                    if heartbeat_dependency_failed[0]:
                        raise CampaignValidationError("redis_unavailable")
                    raise IdentityPreflightStale()
                campaign = self._store.get_campaign(campaign_id)
                if (
                    campaign is None or campaign.get("status") != "running"
                    or campaign.get("identity_generation") != expected_identity_generation
                    or not await self.refresh_leases(refs[:index + 1], campaign_id=campaign_id)
                ):
                    raise IdentityPreflightStale()
            if self._tiler is not None:
                value = self._tiler(bindings)
                if hasattr(value, "__await__"):
                    await value
            if heartbeat_lost.is_set():
                if heartbeat_dependency_failed[0]:
                    raise CampaignValidationError("redis_unavailable")
                raise IdentityPreflightStale()
            return bindings
        except Exception as exc:
            failure = exc
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
        closed = await self.close_many(started)
        await self._release_unstarted_leases(refs, raw_ids, started, closed)
        await self._quarantine_unclosed(refs, raw_ids, closed, None)
        if any(not value for value in closed.values()):
            raise CampaignValidationError("profile_close_failed")
        if isinstance(failure, (IdentityPreflightStale, CampaignValidationError)):
            raise failure
        raise failure

    async def close_many(self, raw_ids: Sequence[str]) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for raw_id in raw_ids:
            confirmed = False
            for _ in range(self._close_attempts):
                try:
                    await self._adspower.stop(raw_id)
                    if not await self._adspower.is_active(raw_id):
                        confirmed = True
                        break
                except Exception:
                    continue
            results[raw_id] = confirmed
            ref = self._raw_to_ref.get(raw_id)
            if confirmed and ref is not None:
                self._raw_to_ref.pop(raw_id, None)
                self._confirmed_closed_refs.add(ref)
                await self._release(f"profile:{ref}")
        return results

    async def close_bindings(self, bindings: Sequence[Any]) -> dict[str, bool]:
        raw_ids = [str(binding.profile_id) for binding in bindings]
        closed = await self.close_many(raw_ids)
        refs = [self._raw_to_ref.get(raw_id, raw_id) for raw_id in raw_ids]
        await self._quarantine_unclosed(refs, raw_ids, closed, None)
        return closed

    def _discover(self) -> list[dict[str, str]]:
        if self._profile_discovery is None:
            return []
        try:
            source = self._profile_discovery()
        except Exception as exc:
            raise CampaignValidationError("adspower_unavailable") from exc
        rows: list[dict[str, str]] = []
        for row in source:
            if not isinstance(row, dict) or not all(isinstance(row.get(key), str) for key in ("id", "name", "status")) or not row["id"]:
                raise CampaignValidationError("adspower_unavailable")
            rows.append({"id": row["id"], "name": row["name"], "status": row["status"]})
        return rows

    async def _acquire(self, raw_id: str) -> None:
        if self._lease_factory is None:
            return
        lease = self._lease_factory(raw_id)
        try:
            value = lease.acquire()
            if hasattr(value, "__await__"):
                value = await value
        except Exception:
            raise CampaignValidationError("redis_unavailable") from None
        if not value:
            raise CampaignValidationError("profile_start_failed")
        self._leases[raw_id] = lease
        if raw_id.startswith("profile:"):
            self._confirmed_closed_refs.discard(raw_id.removeprefix("profile:"))

    async def _release(self, raw_id: str) -> None:
        lease = self._leases.pop(raw_id, None)
        if lease is None:
            return
        try:
            value = lease.release()
            if hasattr(value, "__await__"):
                await value
        except Exception:
            return

    async def _release_unstarted_leases(self, refs: Sequence[str], raw_ids: Sequence[str], started: Sequence[str], closed: dict[str, bool]) -> None:
        started_ids = set(started)
        for ref, raw_id in zip(refs, raw_ids, strict=True):
            if raw_id not in started_ids or closed.get(raw_id) is True:
                await self._release(f"profile:{ref}")

    async def refresh_leases(self, profile_refs: Sequence[str], *, campaign_id: str | None = None) -> bool:
        if self._lease_factory is None:
            return True
        keys = ([f"campaign:{campaign_id}"] if campaign_id else []) + [f"profile:{ref}" for ref in profile_refs]
        for key in keys:
            lease = self._leases.get(key)
            if lease is None:
                if key.startswith("profile:") and key.removeprefix("profile:") in self._confirmed_closed_refs:
                    continue
                return False
            try:
                value = lease.refresh()
                if hasattr(value, "__await__"):
                    value = await value
                if not value:
                    return False
            except Exception:
                raise CampaignValidationError("redis_unavailable") from None
        return True

    async def _heartbeat_leases(
        self, keys: Sequence[str], lost: asyncio.Event,
        dependency_failed: list[bool] | None = None,
    ) -> None:
        while True:
            await asyncio.sleep(self._lease_heartbeat_seconds)
            for key in keys:
                lease = self._leases.get(key)
                if lease is None:
                    lost.set()
                    return
                try:
                    value = lease.refresh()
                    if hasattr(value, "__await__"):
                        value = await value
                    if not value:
                        lost.set()
                        return
                except Exception:
                    if dependency_failed is not None:
                        dependency_failed[0] = True
                    lost.set()
                    return

    async def release_campaign_lease(self, campaign_id: str) -> None:
        await self._release(f"campaign:{campaign_id}")

    async def close(self) -> None:
        """Release only confirmed/idle resources and stop the owned Playwright client."""
        if self._raw_to_ref:
            await self.close_many(list(self._raw_to_ref))
        closer = getattr(self._sessions, "close", None)
        if callable(closer):
            value = closer()
            if hasattr(value, "__await__"):
                await value

    async def _quarantine_unclosed(self, refs: Sequence[str], raw_ids: Sequence[str], closed: dict[str, bool], campaign_id: str | None) -> None:
        for ref, raw_id in zip(refs, raw_ids, strict=True):
            if raw_id in closed and not closed[raw_id]:
                metadata = self._store.get_profile_metadata(ref)
                if metadata is not None:
                    self._store.upsert_profile_metadata(**{**metadata, "enabled": False, "health_status": "unhealthy"})
