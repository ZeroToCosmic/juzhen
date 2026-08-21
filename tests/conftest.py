from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from flask.testing import FlaskClient
from sqlalchemy.engine import make_url
from werkzeug.security import generate_password_hash

from gateway.app import create_app
from gateway.auth_store import AuthStore
from gateway.management_db import open_management_db


AUTH_PASSWORD = "valid password 123"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LEGACY_APP_MODULES = frozenset(
    {
        "test_account_routes",
        "test_app",
        "test_browser_routes",
        "test_browser_strategy_config",
        "test_buffer_publish",
        "test_console",
        "test_content_publish",
        "test_ip_check",
        "test_settings_routes",
        "test_tiktok_stats_restart_persistence",
        "test_tiktok_stats_routes",
    }
)
_PRODUCTION_CAMPAIGN_DB_ERROR = (
    "pytest blocked the production Comment Campaign database"
)


def _normalized_sqlite_database_path(database_url):
    try:
        url = make_url(str(database_url))
    except Exception:
        return None
    if url.get_backend_name() != "sqlite":
        return None
    database = unquote(str(url.database or ""))
    if not database or database == ":memory:":
        return None
    return os.path.normcase(str(Path(database).expanduser().resolve()))


def _is_production_campaign_database(database_url):
    selected = _normalized_sqlite_database_path(database_url)
    production = os.path.normcase(str(
        (
            Path(__file__).resolve().parents[1]
            / "data"
            / "comment_campaign"
            / "comment_campaign.db"
        ).resolve()
    ))
    return selected == production


def _install_campaign_store_production_guard(config):
    from comment_campaign.store import CampaignStore

    original = CampaignStore.__init__

    def guarded_init(self, database_url):
        if _is_production_campaign_database(database_url):
            raise AssertionError(_PRODUCTION_CAMPAIGN_DB_ERROR)
        original(self, database_url)

    CampaignStore.__init__ = guarded_init
    config._comment_campaign_store_original_init = original


def _restore_campaign_store_production_guard(config):
    original = getattr(config, "_comment_campaign_store_original_init", None)
    if original is None:
        return
    from comment_campaign.store import CampaignStore

    CampaignStore.__init__ = original
    del config._comment_campaign_store_original_init


def _production_campaign_db_snapshot():
    """Read the production Campaign DB without creating, migrating, or writing it."""
    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "comment_campaign"
        / "comment_campaign.db"
    )
    if not path.exists():
        return None
    stat = path.stat()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        counts = (
            connection.execute("SELECT COUNT(*) FROM comment_templates").fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM comment_template_revisions"
            ).fetchone()[0],
        )
    finally:
        connection.close()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat.st_size,
        stat.st_mtime_ns,
        counts,
    )


def pytest_sessionstart(session):
    _install_campaign_store_production_guard(session.config)
    session.config._comment_campaign_db_guard_enabled = True
    session.config._comment_campaign_db_baseline = _production_campaign_db_snapshot()


def pytest_sessionfinish(session, exitstatus):
    del exitstatus
    try:
        if not getattr(session.config, "_comment_campaign_db_guard_enabled", False):
            return
        baseline = getattr(session.config, "_comment_campaign_db_baseline", None)
        if baseline == _production_campaign_db_snapshot():
            return
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("=", "production Comment Campaign DB changed")
    finally:
        _restore_campaign_store_production_guard(session.config)


class _ForbiddenSubmit:
    def __init__(self):
        self.attempts = 0

    def click(self, *_args, **_kwargs):
        self.attempts += 1
        raise AssertionError("real submit click forbidden in Comment Campaign tests")


@pytest.fixture(autouse=True)
def comment_campaign_external_tripwires(request, monkeypatch):
    if not request.node.path.name.startswith("test_comment_campaign_"):
        return

    from adspower import AdsPowerController
    import comment_campaign.executor as executor_module
    from comment_campaign.executor import CommentExecutor
    from execution_v2.session import PlaywrightSessionFactory
    import requests

    def forbidden_requests(*_args, **_kwargs):
        raise AssertionError("real HTTP forbidden in Comment Campaign tests")

    def forbidden_start(*_args, **_kwargs):
        raise AssertionError("real AdsPower start forbidden in Comment Campaign tests")

    async def forbidden_connect(*_args, **_kwargs):
        raise AssertionError(
            "real Playwright/CDP connect forbidden in Comment Campaign tests"
        )

    submit = _ForbiddenSubmit()

    original_open_scoped_reply = executor_module.open_scoped_reply

    async def forbidden_scoped_reply(*args, **kwargs):
        scope = await original_open_scoped_reply(*args, **kwargs)
        return {**scope, "submit": submit}

    async def forbidden_submit_locator(*_args, **_kwargs):
        return submit

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_requests)
    monkeypatch.setattr(AdsPowerController, "start_browser", forbidden_start)
    monkeypatch.setattr(PlaywrightSessionFactory, "connect", forbidden_connect)
    monkeypatch.setattr(CommentExecutor, "_submit_locator", forbidden_submit_locator)
    monkeypatch.setattr(executor_module, "open_scoped_reply", forbidden_scoped_reply)
    request.node._comment_campaign_external_bombs = SimpleNamespace(
        http=requests.post,
        adspower=AdsPowerController(),
        connect=PlaywrightSessionFactory.connect,
        submit=submit,
        submit_locator=CommentExecutor._submit_locator,
    )


@pytest.fixture
def external_bombs(request):
    return request.node._comment_campaign_external_bombs


def _isolated_comment_campaign_config(tmp_path):
    campaign_root = tmp_path / "comment-campaign"
    database_path = campaign_root / "comment_campaign.db"
    return {
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{database_path.as_posix()}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": campaign_root / "evidence",
    }


class AuthenticatedClient:
    def __init__(self, client, csrf_token, password=AUTH_PASSWORD):
        self.client = client
        self.csrf_token = csrf_token
        self.password = password

    def open(self, path, *, method="GET", **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers.setdefault("X-CSRF-Token", self.csrf_token)
        return self.client.open(
            path,
            method=method,
            headers=headers,
            **kwargs,
        )

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


class _LegacyAdministratorClient(FlaskClient):
    """Test-only client that authenticates through the real login route."""

    username = "legacy-admin"
    password = AUTH_PASSWORD

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.csrf_token = ""

        login_page = self.get("/login")
        assert login_page.status_code == 200
        with self.session_transaction() as values:
            self.csrf_token = values["csrf_token"]

        login = self.post(
            "/api/auth/login",
            json={"username": self.username, "password": self.password},
        )
        assert login.status_code == 200
        self.csrf_token = login.get_json()["csrf_token"]

        session_response = self.get("/api/auth/session")
        assert session_response.status_code == 200
        assert session_response.get_json()["csrf_token"] == self.csrf_token

    def open(self, *args, **kwargs):
        method = str(kwargs.get("method") or "GET").upper()
        if method in _UNSAFE_METHODS and self.csrf_token:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("X-CSRF-Token", self.csrf_token)
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


@dataclass(frozen=True)
class AuthApp:
    app: object
    database_path: object
    state_dir: object


@pytest.fixture(scope="session")
def _legacy_admin_password_hash():
    return generate_password_hash(AUTH_PASSWORD, method="scrypt")


@pytest.fixture(autouse=True)
def authenticate_legacy_app_clients(
    request,
    monkeypatch,
    tmp_path,
    _legacy_admin_password_hash,
):
    """Replace legacy modules' local app factory with an authenticated test factory."""
    module = request.module
    module_name = module.__name__.rsplit(".", 1)[-1]
    if module_name not in _LEGACY_APP_MODULES:
        return

    state_dir = tmp_path / "legacy-management-state"
    database_path = state_dir / "management.db"
    connection = open_management_db(database_path)
    try:
        AuthStore(connection).create_user(
            _LegacyAdministratorClient.username,
            _legacy_admin_password_hash,
            "administrator",
            must_change_password=False,
        )
    finally:
        connection.close()

    def authenticated_create_app(config=None):
        selected_config = dict(config or {})
        selected_config.update(
            {
                "MANAGEMENT_STATE_DIR": state_dir,
                "MANAGEMENT_DB_PATH": database_path,
                **_isolated_comment_campaign_config(tmp_path),
            }
        )
        app = create_app(selected_config)
        app.test_client_class = _LegacyAdministratorClient
        return app

    monkeypatch.setattr(module, "create_app", authenticated_create_app)


@pytest.fixture
def auth_app(tmp_path):
    return _create_auth_app(tmp_path, "admin", "administrator", False)


@pytest.fixture
def client(auth_app):
    return auth_app.app.test_client()


@pytest.fixture
def admin_client(tmp_path):
    context = _create_auth_app(
        tmp_path,
        "admin",
        "administrator",
        False,
    )
    return _login(context.app, "admin", AUTH_PASSWORD)


@pytest.fixture
def operator_client(tmp_path):
    context = _create_auth_app(
        tmp_path,
        "operator",
        "operator",
        False,
    )
    return _login(context.app, "operator", AUTH_PASSWORD)


@pytest.fixture
def must_change_client(tmp_path):
    temporary_password = "temporary password 123"
    context = _create_auth_app(
        tmp_path,
        "temporary",
        "operator",
        True,
        temporary_password,
    )
    return _login(
        context.app,
        "temporary",
        temporary_password,
    )


def _create_auth_app(
    tmp_path,
    username,
    role,
    must_change,
    password=AUTH_PASSWORD,
):
    state_dir = tmp_path / "management-state"
    database_path = state_dir / "management.db"
    connection = open_management_db(database_path)
    try:
        AuthStore(connection).create_user(
            username,
            generate_password_hash(password, method="scrypt"),
            role,
            must_change_password=must_change,
        )
    finally:
        connection.close()
    app = create_app(
        {
            "TESTING": True,
            "MANAGEMENT_STATE_DIR": state_dir,
            "MANAGEMENT_DB_PATH": database_path,
            **_isolated_comment_campaign_config(tmp_path),
        }
    )
    return AuthApp(app, database_path, state_dir)


def _login(app, username, password):
    raw_client = app.test_client()
    assert raw_client.get("/login").status_code == 200
    with raw_client.session_transaction() as values:
        pre_login_csrf = values["csrf_token"]
    response = raw_client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": pre_login_csrf},
    )
    assert response.status_code == 200
    rotated_csrf = response.get_json()["csrf_token"]
    session_response = raw_client.get("/api/auth/session")
    assert session_response.status_code == 200
    assert session_response.get_json()["csrf_token"] == rotated_csrf
    return AuthenticatedClient(
        raw_client,
        rotated_csrf,
        password,
    )
