# SQLite Accounts Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `init_db.py` to create the local `accounts.db` SQLite database for the account status roster.

**Architecture:** Keep database initialization as a root-level Python script that can be run directly or imported by tests. Use Python's standard `sqlite3` module and avoid adding new dependencies.

**Tech Stack:** Python, SQLite, pytest.

## Global Constraints

- Script name: `init_db.py`.
- Default database name: `accounts.db`.
- Table name: `accounts`.
- Required fields: `id`, `ads_power_user_id`, `buffer_account_id`, `proxy_session`, `last_interact_date`, `status`.
- `status` must represent `active` or `banned`.

---

### Task 1: Create Account Database Initializer

**Files:**
- Create: `tests/test_init_db.py`
- Create: `init_db.py`

**Interfaces:**
- Produces: `init_db(db_path: str | Path = "accounts.db") -> Path`
- Produces: CLI behavior `python init_db.py` creates `accounts.db` in the current working directory.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3

import pytest

from init_db import init_db


def test_init_db_creates_accounts_table_with_expected_schema(tmp_path):
    db_path = tmp_path / "accounts.db"

    result = init_db(db_path)

    assert result == db_path
    conn = sqlite3.connect(db_path)
    columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
    conn.close()

    assert [column[1] for column in columns] == [
        "id",
        "ads_power_user_id",
        "buffer_account_id",
        "proxy_session",
        "last_interact_date",
        "status",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_init_db.py -q --basetemp=work\pytest-tmp-<random>`

Expected: FAIL because `init_db` module does not exist.

- [ ] **Step 3: Implement script**

```python
from pathlib import Path
import sqlite3

DEFAULT_DB_PATH = Path("accounts.db")


def init_db(db_path=DEFAULT_DB_PATH):
    db_path = Path(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ads_power_user_id TEXT NOT NULL,
                buffer_account_id TEXT NOT NULL,
                proxy_session TEXT NOT NULL,
                last_interact_date TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'banned'))
            )
            """
        )
    return db_path


if __name__ == "__main__":
    init_db()
```

- [ ] **Step 4: Run verification**

Run: `.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q --basetemp=work\pytest-tmp-<random>`

Expected: all Python tests pass.

Run: `npm.cmd run test:node --cache .\.npm-cache`

Expected: all Node tests pass.
