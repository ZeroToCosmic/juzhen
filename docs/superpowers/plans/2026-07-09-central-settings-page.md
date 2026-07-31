# Central Settings Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize configurable values in a local settings file exposed through a Flask settings page and API, then migrate existing Python and Node code to read those settings.

**Architecture:** Add a `gateway.settings_store` module that loads, merges, and saves `config.json` with safe defaults. Flask exposes `/settings` for editing and `/api/settings` for JSON read/write. Existing proxy, IP, Buffer, and CDP defaults read from the same config shape.

**Tech Stack:** Python, Flask, JSON config file, Node.js

## Global Constraints

- Runtime editable configuration lives in `config.json`.
- `config.json` is ignored by Git because it may contain credentials.
- `config.example.json` documents the supported configuration keys.
- `.env` remains only a fallback for legacy proxy variables and optional `APP_CONFIG_PATH`.
- Existing routes and command-line flows keep working.

---

### Task 1: Settings Store and Page

**Files:**
- Create: `gateway/settings_store.py`
- Create: `config.example.json`
- Modify: `gateway/app.py`
- Modify: `.gitignore`
- Test: `tests/test_settings_store.py`
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Produces: `load_settings(path: str | Path | None = None) -> dict`
- Produces: `save_settings(settings: dict, path: str | Path | None = None) -> dict`
- Produces: `merge_settings(overrides: dict) -> dict`
- Produces: `GET /settings`
- Produces: `GET /api/settings`
- Produces: `PUT /api/settings`

### Task 2: Existing Python Config Migration

**Files:**
- Modify: `gateway/config.py`
- Modify: `gateway/ip_checker.py`
- Modify: `gateway/buffer_client.py`
- Test: existing route and service tests

**Behavior:**
- Proxy generation reads proxy fields from settings, with environment fallback.
- IP check URL and timeout read from settings.
- Buffer URL and timeout read from settings.

### Task 3: Node Config Fallback

**Files:**
- Modify: `browser/cdp.js`
- Test: `tests-js/cdp.test.js`

**Behavior:**
- CLI arguments still override everything.
- If CLI args are missing, `runAgent` can use `browser.cdpUrl` and `browser.taskGoal` from `config.json`.
