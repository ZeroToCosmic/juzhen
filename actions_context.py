"""评论上下文、规则匹配和可配置模型兜底。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import requests


DEFAULT_COMMENT = "Great video! 🔥"
COMMENT_RULES: dict[str, str] = {
    "iphone": "这个细节很有意思，iPhone 用户表示学到了 📱🔥",
    "coffee": "咖啡控看到这里已经坐不住了 ☕️✨",
}


def _metadata_text(metadata: dict[str, Any] | None) -> str:
    metadata = metadata or {}
    description = metadata.get("description") or metadata.get("video_description") or ""
    tags = metadata.get("tags") or metadata.get("hashtags") or []
    if isinstance(tags, (list, tuple, set)):
        tags = " ".join(str(tag) for tag in tags)
    return f"{description} {tags}".lower().strip()


def load_comment_rules(path: str | Path | None = None) -> dict[str, str]:
    """加载自定义规则；JSON 格式为 ``{"keyword": "comment"}``。"""

    rules = dict(COMMENT_RULES)
    configured_path = path or os.getenv("COMMENT_RULES_PATH", "")
    if not configured_path:
        try:
            from gateway.settings_store import load_settings

            configured_rules = load_settings().get("comment_rules", {})
            if isinstance(configured_rules, dict):
                rules.update(
                    {str(key).lower(): str(value) for key, value in configured_rules.items()}
                )
        except Exception:
            pass
    if configured_path:
        rule_path = Path(configured_path)
        if rule_path.exists():
            loaded = json.loads(rule_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("评论规则文件必须是 JSON 对象")
            rules.update({str(key).lower(): str(value) for key, value in loaded.items()})
    return rules


def _active_model() -> dict[str, Any] | None:
    """读取配置页面中启用的模型，优先使用 default_model_id。"""

    try:
        from gateway.settings_store import load_settings

        settings = load_settings()
    except Exception:
        return None
    models = settings.get("models", {})
    items = [item for item in models.get("items", []) if item.get("enabled") is not False]
    default_id = models.get("default_model_id")
    return next((item for item in items if item.get("id") == default_id), None) or (
        items[0] if items else None
    )


def _model_endpoint(model: dict[str, Any]) -> tuple[str, str]:
    base_url = str(model.get("base_url") or "").rstrip("/")
    mode = str(model.get("mode") or "chat").lower()
    if mode == "responses":
        return f"{base_url}/responses", "responses"
    return f"{base_url}/chat/completions", "chat"


def _extract_model_text(payload: dict[str, Any], mode: str) -> str:
    if mode == "responses":
        if payload.get("output_text"):
            return str(payload["output_text"])
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("text"):
                    return str(content["text"])
    choices = payload.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(str(part.get("text", "")) for part in content)
        return str(content)
    return ""


def _parse_comment(text: str) -> str:
    cleaned = str(text or "").strip().removeprefix("```json").removesuffix("```").strip()
    payload = json.loads(cleaned)
    comment = str(payload.get("comment") or "").strip()
    if not comment:
        raise ValueError("模型返回的 comment 为空")
    return comment


def _request_ai_comment(metadata_text: str, model: dict[str, Any]) -> str:
    endpoint, mode = _model_endpoint(model)
    api_key = str(model.get("api_key") or "").strip()
    if not api_key:
        env_name = str(model.get("api_key_env") or "").strip()
        api_key = os.getenv(env_name, "") if env_name else ""
    if not endpoint or not model.get("model"):
        raise ValueError("激活模型缺少 base_url 或 model")

    prompt = (
        "你是一名产品测评达人。根据以下视频描述和标签，写一句有网感、带 Emoji 的短评。"
        '只返回 JSON，不要 Markdown，不要额外解释，格式必须是 {"comment": "..."}。\n\n'
        f"视频描述和标签：{metadata_text}"
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if mode == "responses":
        body = {"model": model["model"], "input": prompt, "temperature": 0.8}
    else:
        body = {
            "model": model["model"],
            "messages": [
                {"role": "system", "content": "你只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.8,
        }
    response = requests.post(endpoint, headers=headers, json=body, timeout=30)
    response.raise_for_status()
    return _parse_comment(_extract_model_text(response.json(), mode))


async def generate_comment(metadata: dict[str, Any] | None) -> tuple[str, bool]:
    """规则优先生成评论，未命中时调用激活模型；返回 ``(评论, is_ai)``。"""

    text = _metadata_text(metadata)
    for keyword, comment in load_comment_rules().items():
        if str(keyword).lower() in text:
            return str(comment), False

    model = _active_model()
    if model:
        try:
            comment = await asyncio.to_thread(_request_ai_comment, text, model)
            return comment, True
        except Exception:
            pass
    return DEFAULT_COMMENT, True


async def comment_and_type(page, selector: str, metadata: dict[str, Any] | None) -> tuple[str, bool]:
    """生成评论并输入指定网页元素。"""

    from actions_dom import type_comment

    comment, is_ai = await generate_comment(metadata)
    await type_comment(page, selector, comment)
    return comment, is_ai


__all__ = [
    "COMMENT_RULES",
    "DEFAULT_COMMENT",
    "generate_comment",
    "load_comment_rules",
    "comment_and_type",
]
