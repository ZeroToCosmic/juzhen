from datetime import datetime, timezone
import csv
from io import StringIO
import json

import requests

from gateway.account_store import (
    assign_proxy_session,
    list_buffer_accounts,
    public_accounts,
    save_buffer_account,
    update_channel_sync_error,
    update_channel_sync,
)
from gateway.proxy_pool import proxy_pool_key, select_proxy_from_pool
from gateway.settings_store import load_settings


DEFAULT_BUFFER_GRAPHQL_URL = "https://api.buffer.com"


def parse_buffer_account_import_text(raw_text):
    lines = [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]
    if not lines:
        return []

    rows = [_parse_import_line(line) for line in lines]
    if not rows:
        return []

    header = [cell.strip().lower() for cell in rows[0]]
    has_header = "buffer_token" in header or "token" in header
    data_rows = rows[1:] if has_header else rows
    accounts = []

    for row in data_rows:
        cells = [cell.strip() for cell in row]
        if not any(cells):
            continue
        if has_header:
            record = dict(zip(header, cells))
            account_name = record.get("account_name") or record.get("name") or ""
            buffer_token = record.get("buffer_token") or record.get("token") or ""
            buffer_api = record.get("buffer_api") or record.get("api") or ""
        else:
            account_name = cells[0] if len(cells) > 0 else ""
            buffer_token = cells[1] if len(cells) > 1 else ""
            buffer_api = cells[2] if len(cells) > 2 else ""
        accounts.append(
            {
                "account_name": account_name,
                "buffer_token": buffer_token,
                "buffer_api": buffer_api,
            }
        )

    return accounts


def _parse_import_line(line):
    delimiter = "\t" if "\t" in line else ","
    return next(csv.reader(StringIO(line), delimiter=delimiter))


def buffer_graphql(api_url, token, query, post=requests.post):
    response = post(
        api_url or DEFAULT_BUFFER_GRAPHQL_URL,
        json={"query": query},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        timeout=30,
    )
    text = response.text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"raw": text}

    if not response.ok:
        raise RuntimeError(
            payload.get("message")
            or payload.get("error")
            or getattr(response, "reason", "")
            or "Buffer request failed"
        )

    if payload.get("errors"):
        raise RuntimeError(
            "; ".join(
                item.get("message", str(item)) if isinstance(item, dict) else str(item)
                for item in payload["errors"]
            )
        )

    return payload


def is_tiktok_channel(channel):
    service = str((channel or {}).get("service", "")).lower()
    descriptor = str((channel or {}).get("descriptor", "")).lower()
    return "tiktok" in service or "tiktok" in descriptor


def discover_channels_for_account(account, graphql=buffer_graphql):
    if not account or not account.get("buffer_token"):
        raise RuntimeError("缺少 Buffer token。")

    settings = load_settings()
    api_url = (
        account.get("buffer_api")
        or settings["services"].get("buffer_graphql_url")
        or DEFAULT_BUFFER_GRAPHQL_URL
    )
    try:
        organizations_payload = graphql(
            api_url,
            account["buffer_token"],
            "query GetOrganizations { account { organizations { id name ownerEmail } } }",
        )
    except RuntimeError as error:
        raise RuntimeError(_buffer_error_context(account, api_url, error)) from error
    organizations = (
        organizations_payload.get("data", {})
        .get("account", {})
        .get("organizations", [])
    )

    all_channels = []
    for organization in organizations:
        organization_id = str(organization.get("id", "")).replace('"', '\\"')
        query = (
            'query GetChannels { channels(input: { organizationId: "'
            + organization_id
            + '" }) { id name displayName service descriptor avatar externalLink '
            + "isQueuePaused isDisconnected isLocked organizationId } }"
        )
        try:
            channels_payload = graphql(api_url, account["buffer_token"], query)
        except RuntimeError as error:
            raise RuntimeError(_buffer_error_context(account, api_url, error)) from error
        channels = channels_payload.get("data", {}).get("channels", [])
        for channel in channels:
            all_channels.append(
                {
                    "id": channel.get("id", ""),
                    "name": channel.get("name", ""),
                    "displayName": channel.get("displayName", ""),
                    "service": channel.get("service", ""),
                    "descriptor": channel.get("descriptor", ""),
                    "avatar": channel.get("avatar", ""),
                    "externalLink": channel.get("externalLink", ""),
                    "isQueuePaused": bool(channel.get("isQueuePaused")),
                    "isDisconnected": bool(channel.get("isDisconnected")),
                    "isLocked": bool(channel.get("isLocked")),
                    "organizationId": organization.get("id")
                    or channel.get("organizationId", ""),
                    "organizationName": organization.get("name", ""),
                }
            )

    tiktok_channels = [channel for channel in all_channels if is_tiktok_channel(channel)]
    return {
        "accountId": account.get("id"),
        "account_name": account.get("account_name"),
        "organizations": organizations,
        "channels": all_channels,
        "tiktok_channels": tiktok_channels,
        "buffer_profile_ids": [channel["id"] for channel in tiktok_channels],
    }


def _buffer_error_context(account, api_url, error):
    return (
        f"{account.get('account_name') or account.get('id') or 'Buffer 账号'} 同步失败："
        f"{error}。请检查 Buffer token 是否有效、是否有访问权限，以及 Buffer API 地址是否正确。"
        f" API: {api_url}; Token: {_mask_token(account.get('buffer_token', ''))}"
    )


def _mask_token(token):
    if not token:
        return ""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}...{token[-4:]}"


def discover_accounts(
    db_path,
    account_id=None,
    discover_account=discover_channels_for_account,
    now_fn=None,
):
    targets = list_buffer_accounts(db_path, account_id)
    if not targets:
        raise RuntimeError(
            "没有找到这个 Buffer 账号。" if account_id else "请先导入 Buffer 账号。"
        )

    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    results = []
    for target in targets:
        try:
            discovery = discover_account(target)
            synced_at = now_fn()
            update_channel_sync(
                db_path,
                target["id"],
                channels=discovery["channels"],
                profile_ids=discovery["buffer_profile_ids"],
                synced_at=synced_at,
                error="",
            )
            results.append({"status": "ok", **discovery})
        except Exception as error:
            update_channel_sync_error(db_path, target["id"], str(error))
            results.append(
                {
                    "status": "failed",
                    "accountId": target["id"],
                    "account_name": target.get("account_name", ""),
                    "error": str(error),
                    "channels": [],
                    "tiktok_channels": [],
                    "buffer_profile_ids": [],
                }
            )

    return {
        "count": len(results),
        "results": results,
        "accounts": public_accounts(db_path),
    }


def import_buffer_accounts(
    db_path,
    *,
    accounts=None,
    raw_text="",
    discover_account=discover_channels_for_account,
    now_fn=None,
):
    targets = [_normalize_import_account(account) for account in (accounts or [])]
    if raw_text:
        targets.extend(parse_buffer_account_import_text(raw_text))
    targets = [target for target in targets if target.get("buffer_token")]

    if not targets:
        raise RuntimeError("请先导入 Buffer 账号和 token。")

    settings = load_settings()
    proxy_pool = settings.get("proxy_pool", {}).get("items", [])
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    results = []
    saved_accounts = 0

    for index, target in enumerate(targets, start=1):
        target.setdefault("id", f"imported-{index}")
        try:
            saved_account = save_buffer_account(db_path, target)
            saved_accounts += 1
            target["id"] = saved_account["id"]
            discovery = discover_account(target)
            synced_at = now_fn()
            profile_ids = discovery.get("buffer_profile_ids", [])
            selected_proxy = select_proxy_from_pool(
                proxy_pool,
                profile_ids[0] if profile_ids else saved_account["id"],
            )
            proxy_session = proxy_pool_key(selected_proxy) if selected_proxy else ""
            assign_proxy_session(db_path, saved_account["id"], proxy_session)
            update_channel_sync(
                db_path,
                saved_account["id"],
                channels=discovery.get("channels", []),
                profile_ids=profile_ids,
                synced_at=synced_at,
                error="",
            )
            results.append({"status": "ok", **discovery})
        except Exception as error:
            results.append(
                {
                    "status": "failed",
                    "accountId": target.get("id", ""),
                    "account_name": target.get("account_name", ""),
                    "error": str(error),
                    "channels": [],
                    "tiktok_channels": [],
                    "buffer_profile_ids": [],
                }
            )

    return {
        "imported": len(targets),
        "saved_accounts": saved_accounts,
        "results": results,
        "accounts": public_accounts(db_path),
    }


def _normalize_import_account(account):
    return {
        "id": str(account.get("id", "")) if account.get("id") else "",
        "account_name": account.get("account_name") or account.get("name") or "",
        "buffer_token": account.get("buffer_token") or account.get("token") or "",
        "buffer_api": account.get("buffer_api") or account.get("api") or "",
    }
