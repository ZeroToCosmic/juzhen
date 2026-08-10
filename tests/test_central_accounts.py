"""Central account import / deploy tests (M1, PRD F26)."""

from __future__ import annotations

import pytest

from central import config, db
from central.models import Account, Base, DeployTask, Device, ImportJob
from central.outbox import OutboxMessage
from central.app import app

from tests.test_central_skeleton import central_client  # noqa: F401


def _register_device(client, tenant, device_id, max_accounts=300, used=0, enabled=True):
    response = client.post(
        "/api/central/devices/heartbeat",
        json={
            "tenant_id": tenant,
            "device_id": device_id,
            "session_id": f"s-{device_id}",
            "max_accounts": max_accounts,
            "used_accounts": used,
        },
    )
    assert response.status_code == 200
    if not enabled:
        client.patch(
            f"/api/central/devices/{device_id}",
            json={"enabled": False},
            headers={"X-Tenant-ID": tenant},
        )


def _import(client, tenant, accounts, dry_run=False):
    return client.post(
        "/api/central/accounts/import",
        json={"accounts": accounts, "dry_run": dry_run},
        headers={"X-Tenant-ID": tenant},
    )


def test_import_creates_accounts_and_deploy_tasks(central_client):
    _register_device(central_client, "tenant-a", "win-01")
    response = _import(
        central_client,
        "tenant-a",
        [
            {"account_id": "acc-1", "tiktok_identity": "@alpha"},
            {"account_id": "acc-2", "tiktok_identity": "@beta"},
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 0

    with db.session_scope() as session:
        accounts = session.query(Account).all()
        assert {a.account_id for a in accounts} == {"acc-1", "acc-2"}
        assert all(a.deploy_status == "DEPLOYING" for a in accounts)
        assert all(a.authoritative_device_id == "win-01" for a in accounts)
        tasks = session.query(DeployTask).all()
        assert len(tasks) == 2
        assert all(t.status == "queued" for t in tasks)
        messages = session.query(OutboxMessage).all()
        assert len(messages) == 2
        assert all(m.subject == "tenant-a/account.deploy" for m in messages)
        device = session.query(Device).one()
        assert device.used_accounts == 2


def test_import_dry_run_creates_nothing(central_client):
    _register_device(central_client, "tenant-a", "win-01")
    response = _import(
        central_client,
        "tenant-a",
        [{"account_id": "acc-1"}],
        dry_run=True,
    )
    assert response.json()["dry_run"] is True
    assert response.json()["succeeded"] == 1
    with db.session_scope() as session:
        assert session.query(Account).count() == 0
        assert session.query(DeployTask).count() == 0
        job = session.query(ImportJob).one()
        assert job.succeeded == 1


def test_import_duplicate_account_rejected(central_client):
    _register_device(central_client, "tenant-a", "win-01")
    _import(central_client, "tenant-a", [{"account_id": "acc-1"}])
    response = _import(central_client, "tenant-a", [{"account_id": "acc-1"}])
    body = response.json()
    assert body["failed"] == 1
    assert body["failures"][0]["reason"] == "duplicate_account"
    with db.session_scope() as session:
        assert session.query(Account).count() == 1
        assert session.query(DeployTask).count() == 1


def test_import_no_device_capacity(central_client):
    _register_device(central_client, "tenant-a", "win-01", max_accounts=1)
    _import(central_client, "tenant-a", [{"account_id": "acc-1"}])
    response = _import(central_client, "tenant-a", [{"account_id": "acc-2"}])
    body = response.json()
    assert body["failed"] == 1
    assert body["failures"][0]["reason"] == "no_device_capacity"
    with db.session_scope() as session:
        assert session.query(DeployTask).count() == 1


def test_import_balances_across_devices_by_water_level(central_client):
    _register_device(central_client, "tenant-a", "win-01", max_accounts=300, used=290)
    _register_device(central_client, "tenant-a", "win-02", max_accounts=300, used=10)
    response = _import(
        central_client,
        "tenant-a",
        [{"account_id": "acc-1"}, {"account_id": "acc-2"}],
    )
    assert response.json()["succeeded"] == 2
    with db.session_scope() as session:
        tasks = session.query(DeployTask).all()
        assigned = {t.account_id: t.device_id for t in tasks}
        assert assigned == {"acc-1": "win-02", "acc-2": "win-02"}
        devices = {d.device_id: d.used_accounts for d in session.query(Device).all()}
        assert devices == {"win-01": 290, "win-02": 12}


def test_import_skips_disabled_devices(central_client):
    _register_device(central_client, "tenant-a", "win-01", enabled=False)
    response = _import(central_client, "tenant-a", [{"account_id": "acc-1"}])
    body = response.json()
    assert body["failed"] == 1
    assert body["failures"][0]["reason"] == "no_device_capacity"


def test_import_job_status_and_account_list(central_client):
    _register_device(central_client, "tenant-a", "win-01")
    job = _import(central_client, "tenant-a", [{"account_id": "acc-1"}]).json()
    status = central_client.get(
        f"/api/central/accounts/import/{job['job_id']}",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert status.json()["succeeded"] == 1

    accounts = central_client.get(
        "/api/central/accounts", headers={"X-Tenant-ID": "tenant-a"}
    ).json()
    assert accounts["count"] == 1
    assert accounts["accounts"][0]["deploy_status"] == "DEPLOYING"

    other = central_client.get(
        "/api/central/accounts", headers={"X-Tenant-ID": "tenant-b"}
    ).json()
    assert other["count"] == 0


def test_allocatable_devices_water_level_view(central_client):
    _register_device(central_client, "tenant-a", "win-01", max_accounts=300, used=150)
    response = central_client.get(
        "/api/central/accounts/devices", headers={"X-Tenant-ID": "tenant-a"}
    )
    body = response.json()
    assert body["count"] == 1
    assert body["devices"][0]["water_level"] == pytest.approx(0.5)
