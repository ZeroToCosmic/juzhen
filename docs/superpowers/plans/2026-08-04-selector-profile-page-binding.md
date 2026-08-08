# Selector Profile/Page Binding Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind each test Profile to one unique CDP browser and one ready TikTok probe Page before discovery.

**Architecture:** Add fail-closed uniqueness checks inside `ProbeSessionManager`. Prepare both healing Pages through existing `ProbeStateRunner` readiness before primary deterministic extraction, while preserving owned-resource cleanup.

**Tech Stack:** Python 3, asyncio, Playwright CDP, pytest.

## Global Constraints

- Add no external API, Redis, database, configuration, or queue changes.
- Store no raw Profile ID or CDP endpoint in progress evidence.
- Keep cleanup limited to resources created by the probe.
- Keep existing three-attempt readiness behavior.

---

### Task 1: Enforce unique session bindings

**Files:**
- Modify: `selector_probe/session.py`
- Test: `tests/test_selector_probe_session.py`

**Interfaces:**
- Keeps `ProbeSessionManager.open_profiles(...) -> list[ProfileHandle]`.
- Keeps `ProbeSessionManager.open_probe_page(...) -> ProbePageHandle`.
- Adds only safe errors `profile_cdp_collision` and `probe_page_duplicate`.

- [ ] Add tests for duplicate CDP endpoint rejection, repeated Page rejection, and one sanitized binding progress event per Profile.
- [ ] Canonicalize CDP endpoint identity in memory; never persist or log it.
- [ ] Track Profile IDs that already own a Page in the manager instance.
- [ ] Run `python -m pytest tests/test_selector_probe_session.py -q`.

### Task 2: Prepare both healing Pages before discovery

**Files:**
- Modify: `selector_probe/healing_runtime.py`
- Test: `tests/test_selector_probe_healing_runtime.py`

**Interfaces:**
- Keeps `HealingRuntime` public methods unchanged.
- Uses existing `_ensure_state_with_retry(..., state="feed_ready")`.

- [ ] Add test proving both masked Profiles reach `feed_ready` during runtime entry.
- [ ] Add test proving deterministic discovery reuses prepared primary state without a second navigation.
- [ ] Open both Pages first, then await readiness for both before `_open()` completes.
- [ ] Run focused healing runtime tests.

### Task 3: Regression verification

**Files:**
- No runtime files beyond Tasks 1 and 2.

- [ ] Run all `test_selector_probe_*.py` tests.
- [ ] Run Selector Probe JavaScript tests.
- [ ] Run `git diff --check`.

