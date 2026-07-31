# Account Roster API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Flask APIs for selecting the next usable account and updating account interaction status in the local SQLite roster.

**Architecture:** Add `gateway/account_store.py` for SQLite queries and keep Flask route handlers thin. Store path comes from `app.config["ACCOUNTS_DB_PATH"]` with fallback to `accounts.db`.

**Tech Stack:** Python, Flask, SQLite, pytest.

## Global Constraints

- `GET /api/account/next` returns an active account whose `last_interact_date` is empty or not today.
- `POST /api/account/update` accepts `ads_power_user_id` and `result`.
- `result` values: `success`, `failed`, `banned`, `abnormal`.
- `success` and `failed` mark today's interaction date and keep `status = active`.
- `banned` and `abnormal` mark today's interaction date and set `status = banned`.

---

### Task 1: Account Store And Flask Routes

**Files:**
- Create: `gateway/account_store.py`
- Create: `tests/test_account_routes.py`
- Modify: `gateway/app.py`

**Interfaces:**
- Produces: `get_next_account(db_path, today=None) -> dict | None`
- Produces: `update_account(db_path, ads_power_user_id, result, today=None) -> dict | None`

- [ ] **Step 1: Write failing route tests**

```python
def test_next_account_returns_active_account_not_interacted_today(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    seed account rows...
    app = create_app()
    app.config["ACCOUNTS_DB_PATH"] = db_path
    response = app.test_client().get("/api/account/next")
    assert response.status_code == 200
    assert response.get_json()["ads_power_user_id"] == "ads-ready"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_account_routes.py -p no:cacheprovider -q --basetemp=work\pytest-tmp-<random>`

Expected: FAIL because routes return 404.

- [ ] **Step 3: Implement account store and routes**

Add `gateway/account_store.py` with SQLite query helpers and add routes to `gateway/app.py`.

- [ ] **Step 4: Run verification**

Run: `.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q --basetemp=work\pytest-tmp-<random>`

Expected: all Python tests pass.

Run: `npm.cmd run test:node --cache .\.npm-cache`

Expected: all Node tests pass.
