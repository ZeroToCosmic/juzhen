# SOCKS5 Proxy Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make imported static proxies use their real SOCKS5 protocol and unchanged credentials while preserving legacy HTTP session proxies.

**Architecture:** Store one protocol setting for the imported proxy pool and combine it with each account's unchanged `host:port:username:password` assignment when building a request URL. Keep `generate_proxy_url(account_id)` as the legacy environment/session path.

**Tech Stack:** Python 3, Flask, Requests with PySocks, pytest, HTML/JavaScript control panel.

## Global Constraints

- Existing account proxy assignments must remain stable when settings are saved.
- Imported proxy usernames and passwords must never be rewritten.
- Legacy environment proxy behavior from Task 1.1.2 must remain compatible.
- Full proxy passwords must not appear in API errors or UI status output.

---

### Task 1: Proxy protocol configuration and URL generation

**Files:**
- Modify: `gateway/settings_store.py`
- Modify: `gateway/app.py`
- Modify: `requirements.txt`
- Test: `tests/test_proxy.py`
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Consumes: account `proxy_session` in `host:port:username:password` format.
- Produces: `build_proxy_url_from_session(account_id, proxy_session, protocol)` returning a Requests-compatible URL.

- [ ] Add failing tests asserting that the default pool protocol is `socks5`, pool URLs use `socks5h://`, and raw usernames remain unchanged.
- [ ] Run the focused tests and confirm they fail because the current implementation returns `http://` with a Session suffix.
- [ ] Add `proxy_pool.protocol`, normalize accepted values to `socks5` or `http`, and build static pool URLs without username mutation.
- [ ] Add `requests[socks]` so Requests can use `socks5h://` URLs.
- [ ] Run focused tests and confirm the legacy `generate_proxy_url` test and new pool tests pass.

### Task 2: Central configuration UI

**Files:**
- Modify: `gateway/app.py`
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: `/api/settings.proxy_pool.protocol`.
- Produces: a `proxy_pool.protocol` select with `socks5` and `http` values in active settings forms.

- [ ] Add a failing page test for the protocol selector and SOCKS5 option.
- [ ] Run the focused test and confirm the selector is absent.
- [ ] Add the compact selector beside the proxy-pool input; existing form serialization and loading continue to handle the dotted field name.
- [ ] Run page and settings API tests and confirm the selected protocol round-trips.

### Task 3: Verification and live proxy check

**Files:**
- Verify: `gateway/ip_checker.py`
- Verify: `gateway/buffer_client.py`

**Interfaces:**
- Consumes: the shared account proxy URL.
- Produces: successful SOCKS5 request routing or a target-service response instead of an HTTP-proxy timeout.

- [ ] Run all Python and Node tests.
- [ ] Run Python compilation and dependency consistency checks.
- [ ] Restart the current Flask service with the updated dependency and code.
- [ ] Call `/check_ip` for one assigned account without printing credentials and confirm the request traverses the proxy.
- [ ] Send a non-mutating Buffer GraphQL identity query through one assigned proxy and confirm the connection succeeds.

## Repository Note

This workspace is not currently a usable Git worktree, so implementation checkpoints cannot be committed. Verification evidence will be reported directly.
