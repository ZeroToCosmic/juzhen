from datetime import datetime, timezone

import requests

from gateway.settings_store import load_settings


DEFAULT_BUFFER_GRAPHQL_URL = "https://api.buffer.com"
DEFAULT_BUFFER_TIMEOUT_SECONDS = 30
CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post {
        id
        text
        dueAt
        assets {
          source
        }
      }
    }
    ... on MutationError {
      message
    }
  }
}
""".strip()
GET_POST_QUERY = """
query GetPost($id: ID!) {
  post(id: $id) {
    id
    status
    url
    permalink
    serviceLink
    serviceUpdateUrl
    externalUrl
    nativeUrl
  }
}
""".strip()


class BufferAPIError(requests.exceptions.RequestException):
    pass


def publish_to_buffer(proxy_url: str, access_token: str, payload: dict) -> dict:
    settings = load_settings()
    api_url = (
        settings["services"].get("buffer_graphql_url")
        or DEFAULT_BUFFER_GRAPHQL_URL
    )
    timeout = settings["timeouts"].get(
        "buffer_publish_seconds",
        DEFAULT_BUFFER_TIMEOUT_SECONDS,
    )
    profile_ids = [str(value) for value in payload.get("profile_ids", []) if value]
    if not profile_ids:
        raise BufferAPIError("缺少 Buffer profile/channel ID")

    posts = []
    for profile_id in profile_ids:
        post_input = build_create_post_input(payload, profile_id)
        try:
            response = requests.post(
                api_url,
                json={
                    "query": CREATE_POST_MUTATION,
                    "variables": {"input": post_input},
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=timeout,
            )
        except requests.exceptions.ReadTimeout as error:
            raise BufferAPIError(
                "代理连接 Buffer 超时，请检查代理协议、认证信息或更换可用代理后重试。"
            ) from error

        response.raise_for_status()
        response_payload = response.json()
        posts.append(_extract_created_post(response_payload))

    update_ids = [post.get("id", "") for post in posts]
    return {
        "success": True,
        "update_id": update_ids[0],
        "update_ids": update_ids,
        "posts": posts,
    }


def fetch_buffer_post(access_token: str, post_id: str, proxy_url: str = "") -> dict:
    settings = load_settings()
    api_url = (
        settings["services"].get("buffer_graphql_url")
        or DEFAULT_BUFFER_GRAPHQL_URL
    )
    timeout = settings["timeouts"].get(
        "buffer_publish_seconds",
        DEFAULT_BUFFER_TIMEOUT_SECONDS,
    )
    response = requests.post(
        api_url,
        json={"query": GET_POST_QUERY, "variables": {"id": str(post_id)}},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def extract_tiktok_url_from_buffer_payload(payload: dict) -> str:
    for value in _walk_json_values(payload):
        if isinstance(value, str):
            text = value.strip()
            if "tiktok.com/" in text:
                return text
    return ""


def _walk_json_values(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_json_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_json_values(nested)
    else:
        yield value


def build_create_post_input(payload: dict, profile_id: str) -> dict:
    post_input = {
        "text": str(payload.get("text") or ""),
        "channelId": profile_id,
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "assets": [],
    }
    media = payload.get("media") or {}
    media_url = media.get("link") or media.get("url")
    if media_url:
        post_input["assets"] = [{"video": {"url": str(media_url)}}]

    scheduled_at = str(payload.get("scheduled_at") or "").strip()
    if scheduled_at:
        post_input["mode"] = "customScheduled"
        post_input["dueAt"] = _to_utc_iso(scheduled_at)

    return post_input


def _to_utc_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BufferAPIError("预计发布时间格式无效") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _extract_created_post(payload: dict) -> dict:
    errors = payload.get("errors") or []
    if errors:
        raise BufferAPIError(
            "; ".join(
                item.get("message", str(item)) if isinstance(item, dict) else str(item)
                for item in errors
            )
        )

    result = payload.get("data", {}).get("createPost") or {}
    if result.get("post"):
        return result["post"]
    if result.get("message"):
        raise BufferAPIError(result["message"])
    raise BufferAPIError("Buffer 返回了无法识别的发布结果")
