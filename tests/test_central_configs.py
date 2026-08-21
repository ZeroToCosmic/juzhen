"""Central versioned config tests (M3 increment 2, PRD F6)."""

from __future__ import annotations

import pytest

from central import db
from central.models import Base, ConfigSetting, ConfigVersion
from central.app import app

from tests.test_central_skeleton import central_client  # noqa: F401


def test_put_and_get_config_roundtrip(central_client):
    put = central_client.put(
        "/api/central/configs/window.concurrency",
        json={"value": {"max_concurrent_windows": 3}, "gray_ratio": 1.0},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert put.status_code == 200
    assert put.json()["version"] == 1

    get = central_client.get(
        "/api/central/configs/window.concurrency",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert get.status_code == 200
    body = get.json()
    assert body["value"] == {"max_concurrent_windows": 3}
    assert body["source"] == "tenant"


def test_put_bumps_version_and_keeps_history(central_client):
    central_client.put(
        "/api/central/configs/window.concurrency",
        json={"value": {"max_concurrent_windows": 3}},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    put = central_client.put(
        "/api/central/configs/window.concurrency",
        json={"value": {"max_concurrent_windows": 5}, "gray_ratio": 0.5},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert put.json()["version"] == 2
    assert put.json()["gray_ratio"] == 0.5

    with db.session_scope() as session:
        setting = session.query(ConfigSetting).one()
        assert setting.version == 2
        assert setting.value == {"max_concurrent_windows": 5}
        history = session.query(ConfigVersion).all()
        assert len(history) == 1
        assert history[0].version == 1
        assert history[0].value == {"max_concurrent_windows": 3}


def test_tenant_overrides_global_config(central_client):
    central_client.put(
        "/api/central/configs/global.setting",
        json={"value": {"level": "global"}, "scope": "global"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    global_response = central_client.get(
        "/api/central/configs/global.setting",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert global_response.json()["source"] == "global"

    central_client.put(
        "/api/central/configs/global.setting",
        json={"value": {"level": "tenant-b"}},
        headers={"X-Tenant-ID": "tenant-b"},
    )
    tenant_b = central_client.get(
        "/api/central/configs/global.setting",
        headers={"X-Tenant-ID": "tenant-b"},
    )
    assert tenant_b.json()["source"] == "tenant"
    assert tenant_b.json()["value"] == {"level": "tenant-b"}

    tenant_a = central_client.get(
        "/api/central/configs/global.setting",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert tenant_a.json()["value"] == {"level": "global"}


def test_list_effective_configs_merges(central_client):
    central_client.put(
        "/api/central/configs/a",
        json={"value": {"from": "global"}, "scope": "global"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    central_client.put(
        "/api/central/configs/b",
        json={"value": {"from": "tenant"}},
        headers={"X-Tenant-ID": "tenant-b"},
    )
    body = central_client.get(
        "/api/central/configs",
        headers={"X-Tenant-ID": "tenant-b"},
    ).json()
    keys = {item["key"]: item for item in body["configs"]}
    assert set(keys) == {"a", "b"}
    assert keys["a"]["source"] == "global"
    assert keys["b"]["source"] == "tenant"


def test_get_missing_config_404(central_client):
    response = central_client.get(
        "/api/central/configs/nope",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 404


def test_gray_ratio_validation(central_client):
    response = central_client.put(
        "/api/central/configs/a",
        json={"value": {}, "gray_ratio": 1.5},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 422
