import datetime
import hashlib
import hmac
from urllib.parse import quote
from xml.etree import ElementTree

import requests


VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm")
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


def list_r2_video_objects(settings, get=requests.get):
    r2 = settings.get("r2", {})
    if r2.get("access_key_id") and r2.get("secret_access_key"):
        return _list_r2_video_objects_s3(r2, get)

    endpoint_url = (r2.get("endpoint_url") or "").rstrip("/")
    account_id = r2.get("account_id", "")
    account_token = r2.get("account_token", "")
    bucket = r2.get("bucket", "")
    prefix = r2.get("prefix", "")
    public_base_url = (r2.get("public_base_url") or "").rstrip("/")

    if not endpoint_url and account_id and bucket:
        endpoint_url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/r2/buckets/{bucket}/objects"
        )

    if not endpoint_url or not account_token or not bucket:
        raise ValueError("R2 account_id, account_token, and bucket are required")

    response = get(
        endpoint_url,
        headers={"Authorization": f"Bearer {account_token}"},
        params={"prefix": prefix, "per_page": 1000},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    raw_items = payload.get("videos") or payload.get("objects") or payload.get("result") or []
    videos = []

    for item in raw_items:
        key = item.get("key") or item.get("name") or ""
        url = item.get("url") or item.get("public_url") or ""
        if not url and public_base_url and key:
            url = _object_url(public_base_url, "", key)
        if key.lower().endswith(VIDEO_EXTENSIONS) or url.lower().endswith(VIDEO_EXTENSIONS):
            videos.append({"key": key, "url": url})

    return videos


def _list_r2_video_objects_s3(r2, get):
    account_id = r2.get("account_id", "")
    access_key_id = r2.get("access_key_id", "")
    secret_access_key = r2.get("secret_access_key", "")
    bucket = r2.get("bucket", "")
    prefix = r2.get("prefix", "")
    public_base_url = (r2.get("public_base_url") or "").rstrip("/")
    if not account_id or not access_key_id or not secret_access_key or not bucket:
        raise ValueError("R2 account_id, access_key_id, secret_access_key, and bucket are required")

    host = f"{account_id}.r2.cloudflarestorage.com"
    url = f"https://{host}/{bucket}"
    params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
    headers = _s3_headers(
        method="GET",
        host=host,
        path=f"/{bucket}",
        params=params,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )
    response = get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    raw_xml = getattr(response, "content", None) or response.text
    keys = _parse_s3_list_keys(raw_xml)
    return [
        {"key": key, "url": _object_url(public_base_url, url, key)}
        for key in keys
        if key.lower().endswith(VIDEO_EXTENSIONS)
    ]


def _s3_headers(
    *,
    method,
    host,
    path,
    params,
    access_key_id,
    secret_access_key,
):
    timestamp = datetime.datetime.now(datetime.UTC)
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")
    region = "auto"
    service = "s3"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    canonical_query = "&".join(
        f"{quote(str(key), safe='')}={quote(str(params[key]), safe='')}"
        for key in sorted(params)
    )
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{EMPTY_BODY_SHA256}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [
            method,
            quote(path, safe="/"),
            canonical_query,
            canonical_headers,
            signed_headers,
            EMPTY_BODY_SHA256,
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _signature_key(secret_access_key, date_stamp, region, service)
    signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": (
            "AWS4-HMAC-SHA256 "
            f"Credential={access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        ),
        "x-amz-content-sha256": EMPTY_BODY_SHA256,
        "x-amz-date": amz_date,
    }


def _signature_key(secret_access_key, date_stamp, region, service):
    key = ("AWS4" + secret_access_key).encode("utf-8")
    date_key = hmac.new(key, date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _parse_s3_list_keys(raw_xml):
    root = ElementTree.fromstring(raw_xml)
    keys = []
    for element in root.iter():
        if element.tag.endswith("Key") and element.text:
            keys.append(element.text)
    return keys


def _object_url(public_base_url, fallback_bucket_url, key):
    encoded_key = quote(key.lstrip("/"), safe="/()")
    if public_base_url:
        return f"{public_base_url}/{encoded_key}"
    return f"{fallback_bucket_url}/{encoded_key}"
