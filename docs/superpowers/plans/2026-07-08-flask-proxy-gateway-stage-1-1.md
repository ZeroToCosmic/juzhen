# Flask Proxy Gateway Stage 1.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a small modular Flask service that loads proxy settings from `.env` and returns `{"status": "ok"}` from `/ping`.

**Architecture:** Keep the gateway as a Python package with an application factory and a separate configuration module. Use `python-dotenv` for `.env` loading and Flask's built-in test client for verification.

**Tech Stack:** Python, Flask, python-dotenv, pytest

## Global Constraints

- The service runs locally on `127.0.0.1:5000`.
- The health endpoint is `GET /ping`.
- `/ping` returns exactly `{"status": "ok"}` as JSON.
- Proxy variables are `PROXY_HOST`, `PROXY_PORT`, `PROXY_USER`, and `PROXY_PASS`.

---

### Task 1: Flask Gateway Skeleton

**Files:**
- Create: `gateway/__init__.py`
- Create: `gateway/app.py`
- Create: `gateway/config.py`
- Create: `app.py`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `tests/test_app.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `gateway.app.create_app() -> flask.Flask`
- Produces: `gateway.config.ProxyConfig`
- Produces: `gateway.config.load_proxy_config() -> ProxyConfig`

- [ ] **Step 1: Write failing route test**

```python
from gateway.app import create_app


def test_ping_returns_ok_status():
    app = create_app()
    client = app.test_client()

    response = client.get("/ping")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
```

- [ ] **Step 2: Write failing config test**

```python
from gateway.config import load_proxy_config


def test_load_proxy_config_reads_environment(monkeypatch):
    monkeypatch.setenv("PROXY_HOST", "proxy.example.com")
    monkeypatch.setenv("PROXY_PORT", "8080")
    monkeypatch.setenv("PROXY_USER", "session-user")
    monkeypatch.setenv("PROXY_PASS", "secret")

    config = load_proxy_config()

    assert config.host == "proxy.example.com"
    assert config.port == "8080"
    assert config.username == "session-user"
    assert config.password == "secret"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest -q`

Expected: tests fail because the `gateway` package does not exist yet.

- [ ] **Step 4: Implement minimal service and config**

Create a Flask app factory with `/ping`, a dataclass config loader, local entrypoint, dependency file, and `.env.example`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest -q`

Expected: both tests pass.

- [ ] **Step 6: Verify the HTTP service manually**

Run the service on `127.0.0.1:5000`, request `/ping`, and confirm the response body is `{"status":"ok"}`.
