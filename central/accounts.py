"""Account import and deploy orchestration (PRD F26, M1 central side)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from central.allocation import select_device
from central.db import get_session
from central.models import Account, DeployTask, Device, ImportJob
from central.outbox import add_outbox
from central.security import require_tenant

router = APIRouter(prefix="/api/central/accounts", tags=["accounts"])

ACCOUNT_DEPLOY_STATUSES = {
    "IMPORTED",
    "DEPLOYING",
    "WAITING_LOGIN",
    "ACTIVE",
    "FAILED",
}


class AccountImportItem(BaseModel):
    account_id: str = Field(min_length=1, max_length=128)
    tiktok_identity: str = Field(default="", max_length=256)
    ads_power_params: dict = Field(default_factory=dict)


class AccountImportRequest(BaseModel):
    accounts: list[AccountImportItem] = Field(min_length=1, max_length=5000)
    dry_run: bool = False


def account_view(account: Account) -> dict:
    return {
        "tenant_id": account.tenant_id,
        "account_id": account.account_id,
        "profile_id": account.profile_id,
        "tiktok_identity": account.tiktok_identity,
        "deploy_status": account.deploy_status,
        "business_status": account.business_status,
        "authoritative_device_id": account.authoritative_device_id,
        "import_job_id": account.import_job_id,
        "revision": account.revision,
    }


@router.post("/import")
def import_accounts(
    payload: AccountImportRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    job_id = uuid.uuid4().hex
    job = ImportJob(
        id=job_id,
        tenant_id=tenant_id,
        total=len(payload.accounts),
        dry_run=payload.dry_run,
    )
    session.add(job)
    succeeded = 0
    failed = 0
    failures: list[dict] = []

    for item in payload.accounts:
        existing = (
            session.query(Account)
            .filter(Account.tenant_id == tenant_id, Account.account_id == item.account_id)
            .one_or_none()
        )
        if existing is not None:
            failed += 1
            failures.append({"account_id": item.account_id, "reason": "duplicate_account"})
            continue

        if payload.dry_run:
            succeeded += 1
            continue

        account = Account(
            tenant_id=tenant_id,
            account_id=item.account_id,
            tiktok_identity=item.tiktok_identity,
            deploy_status="IMPORTED",
            import_job_id=job_id,
        )
        session.add(account)
        session.flush()

        device = select_device(session, tenant_id=tenant_id)
        if device is None:
            failed += 1
            failures.append({"account_id": item.account_id, "reason": "no_device_capacity"})
            continue

        deploy = DeployTask(
            tenant_id=tenant_id,
            account_id=item.account_id,
            device_id=device.device_id,
        )
        session.add(deploy)
        device.used_accounts += 1
        account.deploy_status = "DEPLOYING"
        account.authoritative_device_id = device.device_id
        add_outbox(
            session,
            tenant_id=tenant_id,
            aggregate="account",
            subject=f"{tenant_id}/account.deploy",
            payload={
                "account_id": item.account_id,
                "device_id": device.device_id,
                "ads_power_params": item.ads_power_params,
            },
        )
        succeeded += 1

    job.succeeded = succeeded
    job.failed = failed
    return {
        "job_id": job_id,
        "total": job.total,
        "succeeded": succeeded,
        "failed": failed,
        "dry_run": payload.dry_run,
        "failures": failures,
    }


@router.get("/import/{job_id}")
def import_job_status(
    job_id: str,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    job = (
        session.query(ImportJob)
        .filter(ImportJob.id == job_id, ImportJob.tenant_id == tenant_id)
        .one_or_none()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="import job not found")
    return {
        "job_id": job.id,
        "total": job.total,
        "succeeded": job.succeeded,
        "failed": job.failed,
        "dry_run": job.dry_run,
    }


@router.get("")
def list_accounts(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    accounts = session.query(Account).filter(Account.tenant_id == tenant_id).all()
    return {"count": len(accounts), "accounts": [account_view(a) for a in accounts]}


@router.get("/devices")
def list_allocatable_devices(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    devices = (
        session.query(Device)
        .filter(Device.tenant_id == tenant_id, Device.enabled.is_(True))
        .all()
    )
    return {
        "count": len(devices),
        "devices": [
            {
                "device_id": d.device_id,
                "used_accounts": d.used_accounts,
                "max_accounts": d.max_accounts,
                "water_level": round(d.used_accounts / d.max_accounts, 4) if d.max_accounts > 0 else None,
            }
            for d in devices
        ],
    }
