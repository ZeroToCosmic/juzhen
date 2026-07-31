# Local Management Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add built-in local administrator/operator accounts, secure sessions, CSRF protection, and server-enforced authorization across the complete management dashboard and legacy browser element/strategy APIs.

**Architecture:** A focused management database module owns users and audit events. An authentication service handles scrypt passwords, lockout, session revision, and CSRF; a Flask Blueprint exposes login and account APIs while one application-wide guard protects every non-public route and defaults unsafe legacy endpoints to administrator-only.

**Tech Stack:** Python 3.11+, Flask 3.1.1, Werkzeug scrypt password hashing, SQLite, signed Flask sessions, vanilla JavaScript, pytest.

## Global Constraints

- No default user or default password.
- First administrator is created through a hidden-prompt local CLI.
- Roles are exactly `administrator` and `operator`.
- Five consecutive failures lock an account for 15 minutes.
- Session idle timeout is 30 minutes; absolute timeout is 8 hours.
- Every unsafe request, including login, requires CSRF validation.
- Existing dashboard, element, strategy, and execution routes require authentication.
- Unsafe endpoints default to administrator-only unless explicitly decorated for operators.
- The last enabled administrator cannot be disabled or demoted.
- Passwords, password hashes, session secrets, CSRF tokens, and temporary passwords never enter logs or audit details.
- Repository has no Git metadata. Do not initialize Git or add commit steps without user approval.

---

## File structure

Create:

- `gateway/management_db.py` — durable SQLite connection, migrations, and audit events.
- `gateway/auth_store.py` — user persistence and last-administrator invariants.
- `gateway/auth_service.py` — password, lockout, authentication, session, and CSRF rules.
- `gateway/auth_blueprint.py` — login, logout, account, and session routes.
- `gateway/admin_users.py` — hidden-prompt first-administrator CLI.
- `gateway/session_key.py` — atomic private session-signing key.
- `gateway/templates/login.html` — accessible login page.
- `gateway/static/auth.js` — login and password-change requests.
- `tests/test_management_db.py`
- `tests/test_auth_store.py`
- `tests/test_auth_service.py`
- `tests/test_auth_routes.py`
- `tests/test_admin_users.py`
- `tests/conftest.py`
- `tests-js/auth-ui.test.js`

Modify:

- `gateway/app.py:6317-6339` — session configuration, Blueprint registration,
  global guard, `GET /healthz`.
- `gateway/app.py:6480-6800` — decorate the few operator-allowed existing
  mutations; all other unsafe legacy routes remain administrator-only.
- `gateway/static/dashboard_shell.css` — login/account styles shared with the
  management shell.
- `tests/test_app.py`
- `tests/test_settings_routes.py`
- `tests-js/browser-strategy-ui.test.js`

`requirements.txt` remains unchanged. Flask 3.1.1 already supplies a Werkzeug
version with scrypt support.

## Task 1: Durable users and audit boundary

**Files:**

- Create: `gateway/management_db.py`
- Create: `gateway/auth_store.py`
- Test: `tests/test_management_db.py`
- Test: `tests/test_auth_store.py`

**Interfaces:**

- Produces: `open_management_db(path: Path) -> sqlite3.Connection`.
- Produces: `record_management_audit(connection, *, actor_user_id, event_type, target_type, target_id, result, reason, details) -> int`.
- Produces: `ManagementUser`.
- Produces: `AuthStore`.
- `AuthStore.create_user(username, password_hash, role, must_change_password=True) -> ManagementUser`.
- `AuthStore.get_by_id(user_id) -> ManagementUser | None`.
- `AuthStore.get_by_username(username) -> ManagementUser | None`.
- `AuthStore.update_access(user_id, *, role=None, enabled=None, actor_user_id) -> ManagementUser`.
- `AuthStore.replace_password(user_id, password_hash, *, must_change_password, actor_user_id) -> ManagementUser`.
- `AuthStore.record_login_failure(user_id, now) -> ManagementUser`.
- `AuthStore.record_login_success(user_id, now) -> ManagementUser`.

- [ ] **Step 1: Write failing database and invariant tests**

```python
from gateway.auth_store import AuthStore, LastAdministratorError
from gateway.management_db import open_management_db


def test_usernames_are_case_insensitively_unique(tmp_path):
    connection = open_management_db(tmp_path / "management.db")
    store = AuthStore(connection)
    store.create_user("Admin", "hash-one", "administrator")
    try:
        store.create_user("admin", "hash-two", "operator")
    except ValueError as error:
        assert str(error) == "username_exists"
    else:
        raise AssertionError("duplicate username was accepted")


def test_last_enabled_administrator_cannot_be_disabled_or_demoted(tmp_path):
    connection = open_management_db(tmp_path / "management.db")
    store = AuthStore(connection)
    admin = store.create_user("admin", "hash", "administrator")
    for patch in ({"enabled": False}, {"role": "operator"}):
        try:
            store.update_access(admin.id, actor_user_id=admin.id, **patch)
        except LastAdministratorError as error:
            assert error.code == "last_administrator"
        else:
            raise AssertionError("last administrator invariant was bypassed")


def test_role_change_increments_session_version(tmp_path):
    connection = open_management_db(tmp_path / "management.db")
    store = AuthStore(connection)
    admin = store.create_user("admin", "hash", "administrator")
    operator = store.create_user("ops", "hash", "operator")
    updated = store.update_access(
        operator.id,
        role="administrator",
        actor_user_id=admin.id,
    )
    assert updated.session_version == operator.session_version + 1
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_management_db.py tests/test_auth_store.py -q -p no:cacheprovider
```

Expected: imports fail because the modules do not exist.

- [ ] **Step 3: Implement connection and exact schema**

`open_management_db` must set `row_factory=sqlite3.Row`,
`PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, and
`PRAGMA busy_timeout=5000`.

Apply this idempotent migration:

```sql
CREATE TABLE IF NOT EXISTS management_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('administrator', 'operator')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    session_version INTEGER NOT NULL DEFAULT 1,
    failed_attempt_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS management_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES management_users(id),
    event_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_management_audit_created
ON management_audit_events(created_at DESC, id DESC);
```

Use this immutable projection:

```python
class LastAdministratorError(RuntimeError):
    code = "last_administrator"


@dataclass(frozen=True)
class ManagementUser:
    id: int
    username: str
    password_hash: str
    role: str
    enabled: bool
    must_change_password: bool
    session_version: int
    failed_attempt_count: int
    locked_until: str | None
    last_login_at: str | None
    password_changed_at: str
    created_at: str
    updated_at: str
```

Every store mutation starts `BEGIN IMMEDIATE`, verifies invariants, writes its
audit event in the same transaction, and commits once.

- [ ] **Step 4: Run store tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_management_db.py tests/test_auth_store.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 2: Private session key and first-administrator CLI

**Files:**

- Create: `gateway/session_key.py`
- Create: `gateway/admin_users.py`
- Test: `tests/test_admin_users.py`

**Interfaces:**

- Produces: `load_or_create_session_key(path: Path) -> str`.
- Produces CLI:
  `python -m gateway.admin_users create-admin --username <name>`.
- Consumes: `AuthStore` from Task 1.

- [ ] **Step 1: Write failing key and CLI tests**

```python
from gateway import admin_users
from gateway.session_key import load_or_create_session_key


def test_session_key_is_created_once_and_reused(tmp_path):
    path = tmp_path / "session.key"
    first = load_or_create_session_key(path)
    second = load_or_create_session_key(path)
    assert first == second
    assert len(first) >= 80
    assert path.read_text(encoding="utf-8").strip() == first


def test_create_admin_reads_hidden_password_twice(tmp_path, monkeypatch):
    prompts = []
    values = iter(("correct horse battery staple", "correct horse battery staple"))
    monkeypatch.setattr(
        admin_users.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or next(values),
    )
    code = admin_users.main([
        "create-admin",
        "--username",
        "admin",
        "--database",
        str(tmp_path / "management.db"),
    ])
    assert code == 0
    assert prompts == ["Password: ", "Confirm password: "]


def test_cli_rejects_password_arguments():
    try:
        admin_users.main(["create-admin", "--username", "admin", "--password", "secret"])
    except SystemExit as error:
        assert error.code != 0
    else:
        raise AssertionError("password command-line option was accepted")
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_admin_users.py -q -p no:cacheprovider
```

Expected: imports fail.

- [ ] **Step 3: Implement atomic secret creation**

Use `secrets.token_urlsafe(64)`. Create the parent directory, open the key file
with `os.O_CREAT | os.O_EXCL | os.O_WRONLY`, write UTF-8 plus newline, flush,
and call `os.fsync`. Attempt `os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)`;
failure to tighten permissions is a startup error, not a warning.

If the file exists, require:

- regular file;
- non-empty content;
- at least 80 characters;
- no newline inside the stripped value.

- [ ] **Step 4: Implement the CLI**

Parser subcommands:

```python
create_admin = subparsers.add_parser("create-admin")
create_admin.add_argument("--username", required=True)
create_admin.add_argument(
    "--database",
    default=str(Path("data") / "management.db"),
)
```

Do not define password or password-file options. Read twice with `getpass`.
Require 12 or more characters, hash with:

```python
generate_password_hash(password, method="scrypt")
```

Create the administrator with `must_change_password=False`. Print only:

```text
Administrator created: admin
```

- [ ] **Step 5: Run CLI tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_admin_users.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 3: Authentication, lockout, and session validation

**Files:**

- Create: `gateway/auth_service.py`
- Modify: `gateway/auth_store.py`
- Test: `tests/test_auth_service.py`

**Interfaces:**

- Produces: `AuthError(code, status)`.
- Produces: `AuthService.authenticate(username, password, now) -> ManagementUser`.
- Produces: `AuthService.change_password(user_id, current_password, new_password, now) -> ManagementUser`.
- Produces: `AuthService.create_temporary_user(username, role, actor_user_id, now) -> tuple[ManagementUser, str]`.
- Produces: `AuthService.validate_session(payload, now) -> ManagementUser`.
- Produces: `new_csrf_token() -> str`.

- [ ] **Step 1: Write failing lockout and session tests**

```python
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from gateway.auth_service import AuthError, AuthService
from gateway.auth_store import AuthStore
from gateway.management_db import open_management_db


def service(tmp_path):
    store = AuthStore(open_management_db(tmp_path / "management.db"))
    user = store.create_user(
        "ops",
        generate_password_hash("valid password 123", method="scrypt"),
        "operator",
        must_change_password=False,
    )
    return AuthService(store), user


def test_five_failures_lock_for_fifteen_minutes(tmp_path):
    auth, user = service(tmp_path)
    now = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)
    for _index in range(5):
        with pytest.raises(AuthError) as caught:
            auth.authenticate("ops", "wrong password", now)
        assert caught.value.code == "invalid_credentials"
    locked = auth.store.get_by_id(user.id)
    assert locked.locked_until == (now + timedelta(minutes=15)).isoformat()


def test_session_version_mismatch_is_rejected(tmp_path):
    auth, user = service(tmp_path)
    payload = {
        "user_id": user.id,
        "session_version": user.session_version - 1,
        "issued_at": "2026-07-28T02:00:00+00:00",
        "last_activity_at": "2026-07-28T02:59:00+00:00",
    }
    with pytest.raises(AuthError) as caught:
        auth.validate_session(
            payload,
            datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc),
        )
    assert caught.value.code == "session_revoked"
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_auth_service.py -q -p no:cacheprovider
```

Expected: import failure.

- [ ] **Step 3: Implement exact authentication rules**

Constants:

```python
class AuthError(RuntimeError):
    def __init__(self, code, status):
        super().__init__(code)
        self.code = code
        self.status = status


MAX_FAILURES = 5
LOCK_MINUTES = 15
IDLE_MINUTES = 30
ABSOLUTE_HOURS = 8
MIN_PASSWORD_LENGTH = 12
ALLOWED_ROLES = {"administrator", "operator"}
```

`authenticate`:

1. normalize username with `strip()` but preserve its stored case;
2. load the user;
3. for unknown/disabled users call `check_password_hash(DUMMY_HASH, password)`;
4. reject active lock with `invalid_credentials`;
5. on failure increment the durable counter and set `locked_until` at failure 5;
6. on success clear failures and lock, set `last_login_at`;
7. expose one public code: `invalid_credentials`.

Create one module-level dummy scrypt hash:

```python
DUMMY_HASH = generate_password_hash(
    "management-auth-dummy-password",
    method="scrypt",
)
```

`validate_session` checks enabled user, session version, 30-minute idle timeout,
and 8-hour absolute timeout. Return stable codes `authentication_required`,
`session_revoked`, or `session_expired`.

Temporary passwords use:

```python
secrets.token_urlsafe(18)
```

and are returned once after their scrypt hash is committed.

- [ ] **Step 4: Run service tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_auth_service.py tests/test_auth_store.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 4: Flask session, CSRF, and route guard

**Files:**

- Create: `gateway/auth_blueprint.py`
- Modify: `gateway/app.py:6317-6339`
- Create: `tests/conftest.py`
- Test: `tests/test_auth_routes.py`
- Test: `tests/test_app.py`

**Interfaces:**

- Produces: `create_auth_blueprint(auth_service_factory) -> Blueprint`.
- Produces: `allow_roles(*roles)`.
- Produces: `public_endpoint`.
- Produces: `install_management_guard(app, auth_service_factory)`.
- Sets `g.management_user`.

- [ ] **Step 1: Write failing guard and CSRF tests**

```python
def csrf(client):
    page = client.get("/login")
    assert page.status_code == 200
    with client.session_transaction() as values:
        return values["csrf_token"]


def test_dashboard_redirects_and_api_returns_401(client):
    assert client.get("/").status_code == 302
    response = client.get("/api/browser/elements")
    assert response.status_code == 401
    assert response.get_json()["code"] == "authentication_required"


def test_login_requires_csrf(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "valid password 123"},
    )
    assert response.status_code == 403
    assert response.get_json()["code"] == "csrf_failed"


def test_unsafe_legacy_route_defaults_to_administrator(
    operator_client,
):
    response = operator_client.put(
        "/api/browser/elements",
        json={"elements": {}},
        headers={"X-CSRF-Token": operator_client.csrf_token},
    )
    assert response.status_code == 403
    assert response.get_json()["code"] == "forbidden"
```

Create this shared authenticated-client helper in `tests/conftest.py`:

```python
class AuthenticatedClient:
    def __init__(self, client, csrf_token):
        self.client = client
        self.csrf_token = csrf_token

    def open(self, path, *, method="GET", **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers.setdefault("X-CSRF-Token", self.csrf_token)
        return self.client.open(path, method=method, headers=headers, **kwargs)

    def get(self, path, **kwargs):
        return self.open(path, method="GET", **kwargs)

    def post(self, path, **kwargs):
        return self.open(path, method="POST", **kwargs)

    def put(self, path, **kwargs):
        return self.open(path, method="PUT", **kwargs)

    def patch(self, path, **kwargs):
        return self.open(path, method="PATCH", **kwargs)

    def delete(self, path, **kwargs):
        return self.open(path, method="DELETE", **kwargs)
```

`admin_client` and `operator_client` fixtures:

1. create a temporary management database and application state directory;
2. insert one scrypt-hashed user through `AuthStore`;
3. `GET /login` and read the pre-login CSRF from `session_transaction`;
4. `POST /api/auth/login` with `X-CSRF-Token`;
5. read the rotated token from `GET /api/auth/session`;
6. return `AuthenticatedClient`.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_auth_routes.py tests/test_app.py -k "authentication or csrf or operator" -q -p no:cacheprovider
```

Expected: dashboard remains public or auth routes are missing.

- [ ] **Step 3: Configure the signed session**

In `create_app`:

```python
session_key_path = Path(app.config["MANAGEMENT_STATE_DIR"]) / "session.key"
app.config.update(
    SECRET_KEY=load_or_create_session_key(session_key_path),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(app.config.get("PUBLIC_ORIGIN_HTTPS", False)),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
```

Default `MANAGEMENT_STATE_DIR` to `Path("data")`; tests inject a temporary
directory.

- [ ] **Step 4: Implement decorators and fail-closed guard**

Decorators attach immutable attributes:

```python
def public_endpoint(function):
    function.management_public = True
    return function


def allow_roles(*roles):
    allowed = frozenset(roles)
    if not allowed or not allowed <= {"administrator", "operator"}:
        raise ValueError("invalid management roles")

    def decorate(function):
        function.management_roles = allowed
        return function
    return decorate
```

Guard order:

1. resolve the Flask view function;
2. allow `static` and `GET /healthz`;
3. for every unsafe method, including a public login, validate the session CSRF
   token with `hmac.compare_digest`;
4. allow endpoints marked `management_public`;
5. validate the authenticated session and set `g.management_user`;
6. if `must_change_password`, allow only session, logout, and password-change;
7. safe methods allow both roles unless a decorator is stricter;
8. unsafe methods default to `{"administrator"}`;
9. use decorator roles when present;
10. HTML requests redirect to `/login`; API requests return safe JSON 401/403.

`GET /healthz` returns exactly:

```python
return jsonify({"status": "ok"})
```

- [ ] **Step 5: Add auth routes**

Routes:

```text
GET  /login
GET  /api/auth/session
POST /api/auth/login
POST /api/auth/logout
POST /api/auth/change-password
```

Login rotates CSRF and calls `session.clear()` before writing:

```python
session.update({
    "user_id": user.id,
    "role": user.role,
    "session_version": user.session_version,
    "issued_at": now.isoformat(),
    "last_activity_at": now.isoformat(),
    "csrf_token": new_csrf_token(),
})
```

Session response:

```python
ROLE_PERMISSIONS = {
    "administrator": (
        "management:read",
        "management:write",
        "strategy:execute",
        "probe:run",
        "alert:acknowledge",
        "webhook:test",
        "users:manage",
    ),
    "operator": (
        "management:read",
        "probe:run",
        "alert:acknowledge",
        "webhook:test",
    ),
}


{
    "username": user.username,
    "role": user.role,
    "permissions": list(ROLE_PERMISSIONS[user.role]),
    "must_change_password": user.must_change_password,
    "csrf_token": session["csrf_token"],
}
```

- [ ] **Step 6: Run route tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_auth_routes.py tests/test_app.py -k "authentication or csrf or operator or healthz" -q -p no:cacheprovider -W error
```

Expected: all focused tests pass.

## Task 5: Administrator account APIs

**Files:**

- Modify: `gateway/auth_blueprint.py`
- Modify: `gateway/auth_service.py`
- Test: `tests/test_auth_routes.py`

**Interfaces:**

- Produces:
  `GET /api/admin/users`.
- Produces:
  `POST /api/admin/users`.
- Produces:
  `PATCH /api/admin/users/<int:user_id>`.
- Produces:
  `POST /api/admin/users/<int:user_id>/reset-password`.
- Produces:
  `POST /api/admin/users/<int:user_id>/revoke-sessions`.

- [ ] **Step 1: Write failing role and one-time-password tests**

```python
def test_operator_cannot_list_or_create_users(operator_client):
    assert operator_client.get("/api/admin/users").status_code == 403
    assert operator_client.post(
        "/api/admin/users",
        json={"username": "new-ops", "role": "operator"},
    ).status_code == 403


def test_administrator_creates_one_time_password(admin_client):
    response = admin_client.post(
        "/api/admin/users",
        json={"username": "new-ops", "role": "operator"},
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["user"]["must_change_password"] is True
    assert len(payload["temporary_password"]) >= 20
    listed = admin_client.get("/api/admin/users").get_json()["users"]
    assert "temporary_password" not in str(listed)
    assert "password_hash" not in str(listed)
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_auth_routes.py -k "users or temporary_password" -q -p no:cacheprovider
```

Expected: 404 for account APIs.

- [ ] **Step 3: Implement account routes**

Apply `@allow_roles("administrator")` to all five routes.

Public user projection:

```python
def public_user(user):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "enabled": user.enabled,
        "must_change_password": user.must_change_password,
        "locked_until": user.locked_until,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "revision": user.session_version,
    }
```

`POST` accepts only username and role. Return the temporary password once.
Browser creation is rejected with `secure_origin_required` unless
`request.is_secure` or the hostname is `localhost`, `127.0.0.1`, or `::1`.

`PATCH` requires `expected_revision`, accepts only role and enabled, and returns
`409 stale_revision` on mismatch.

Reset and revoke increment `session_version`. Reset returns a one-time
temporary password and sets `must_change_password=True`.

- [ ] **Step 4: Run account API tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_auth_routes.py tests/test_auth_store.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 6: Login UI and mandatory password change

**Files:**

- Create: `gateway/templates/login.html`
- Create: `gateway/static/auth.js`
- Modify: `gateway/static/dashboard_shell.css`
- Test: `tests-js/auth-ui.test.js`
- Test: `tests/test_auth_routes.py`

**Interfaces:**

- Produces: `createAuthUI(dependencies)`.
- Consumes: Task 4 auth APIs.

- [ ] **Step 1: Write failing Node controller tests**

```javascript
const assert = require("node:assert/strict");
const test = require("node:test");

const {createAuthUI} = require("../gateway/static/auth");


test("login sends csrf and replaces no server text as html", async () => {
  const calls = [];
  const ui = createAuthUI({
    requestJson: async (url, options) => {
      calls.push({url, options});
      return {status: 200, data: {must_change_password: false}};
    },
    csrfToken: () => "csrf-1",
    navigate: () => {},
    setText: (node, value) => { node.textContent = value; },
  });
  const errorNode = {textContent: ""};
  await ui.login("admin", "password", errorNode);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-1");
  assert.equal(errorNode.textContent, "");
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test tests-js/auth-ui.test.js
```

Expected: module is missing.

- [ ] **Step 3: Implement accessible login and change-password UI**

`login.html` uses:

- one `main`;
- explicit username/password labels;
- hidden CSRF field from session;
- `aria-live="polite"` error region;
- no account-enumeration copy;
- no “remember me” option.

`auth.js` is UMD-compatible like `browser_strategy_ui.js`. It uses
`textContent`, never `innerHTML`, and maps every login failure to:

```text
用户名或密码无效，或账号暂时不可用。
```

When `must_change_password=True`, navigate only to the password-change view.

- [ ] **Step 4: Run UI and route tests**

```powershell
node --test tests-js/auth-ui.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_auth_routes.py -k "login or password" -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 7: Protect legacy routes and verify no bypass

**Files:**

- Modify: `gateway/app.py:6317-6800`
- Modify: `tests/test_app.py`
- Modify: `tests/test_settings_routes.py`
- Modify: `tests-js/browser-strategy-ui.test.js`

**Interfaces:**

- Consumes: application-wide guard from Task 4.
- Produces: authenticated behavior for all existing management routes.

- [ ] **Step 1: Add route-matrix tests**

Add one parameterized test with these exact expectations:

```python
@pytest.mark.parametrize(
    ("method", "path", "operator_status"),
    [
        ("GET", "/", 200),
        ("GET", "/api/browser/elements", 200),
        ("PUT", "/api/browser/elements", 403),
        ("GET", "/api/browser/strategies", 200),
        ("PUT", "/api/browser/strategies", 403),
        ("POST", "/api/browser/execute-strategy", 403),
        ("GET", "/api/settings", 200),
        ("PUT", "/api/settings", 403),
    ],
)
def test_operator_route_matrix(operator_client, method, path, operator_status):
    response = operator_client.open(
        path,
        method=method,
        json={} if method != "GET" else None,
    )
    assert response.status_code == operator_status
```

Add administrator equivalents expecting the route's existing non-auth status,
never 401 or 403.

- [ ] **Step 2: Run and verify failures**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_settings_routes.py -k "operator_route_matrix or administrator_route_matrix" -q -p no:cacheprovider
```

Expected: legacy unsafe endpoints are not consistently protected.

- [ ] **Step 3: Mark explicit operator mutations only**

Current legacy element, strategy, settings, and execute routes receive no
operator mutation decorator; they therefore remain administrator-only.

Only authentication mutations are operator-capable in this plan:

```python
@allow_roles("administrator", "operator")
def logout():
    session.clear()
    return jsonify({"status": "logged_out"})


@allow_roles("administrator", "operator")
def change_password():
    payload = request.get_json(silent=True) or {}
    user = current_app.extensions["management_auth_service"].change_password(
        g.management_user.id,
        str(payload.get("current_password") or ""),
        str(payload.get("new_password") or ""),
        datetime.now(timezone.utc),
    )
    session.clear()
    return jsonify({
        "status": "password_changed",
        "user_id": user.id,
        "login_required": True,
    })
```

Selector-probe run-now, acknowledge, and webhook-test routes receive the same
decorator when their Blueprint is implemented in the management-console plan.

Update existing test clients to log in as an administrator through one shared
fixture rather than bypassing the guard.

- [ ] **Step 4: Run full supported suites**

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W error
node --test tests-js/*.test.js
```

Expected: all supported Python and Node tests pass.

## Final acceptance

- [ ] No dashboard or management API is usable without authentication.
- [ ] No default account or password exists.
- [ ] First administrator creation uses hidden input only.
- [ ] Scrypt hashes are used and never serialized.
- [ ] Five failures create a 15-minute durable lock.
- [ ] Idle and absolute session deadlines are enforced.
- [ ] Every unsafe request validates CSRF.
- [ ] Operators cannot mutate legacy elements, strategies, settings, or execute
  strategies.
- [ ] Last-administrator and session-revision invariants pass.
- [ ] Account responses never contain password hashes or stored secrets.
- [ ] Existing authenticated administrator workflows remain green.
