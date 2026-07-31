"""Strict, secret-safe structured-output client for configured models."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import socket
from typing import Callable
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPSConnectionPool


_DEFAULT_REQUEST_FN = requests.post
_MAX_OUTPUT_BYTES = 1_000_000
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 100_000
_MAX_SCHEMA_BYTES = 262_144
_MAX_SCHEMA_NODES = 10_000
_MAX_MESSAGES = 64
_MAX_MESSAGE_BYTES = 131_072
_MAX_MESSAGES_BYTES = 524_288
_MAX_COLLECTION_ITEMS = 1_024
_STRUCTURED_OUTPUT_NAME = "selector_probe"
_MESSAGE_ROLES = {"assistant", "developer", "system", "user"}


class ModelRequestError(RuntimeError):
    """Model call failure that exposes metadata safe for logs and APIs."""

    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status
        message = code if status is None else f"{code} (status={status})"
        super().__init__(message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, status={self.status!r})"
        )


@dataclass(frozen=True, repr=False)
class ModelConfig:
    id: str
    provider: str
    enabled: bool
    base_url: str
    api_key: str
    model: str
    mode: str

    def __repr__(self) -> str:
        return (
            "ModelConfig("
            f"id={self.id!r}, provider={self.provider!r}, enabled={self.enabled!r}, "
            f"mode={self.mode!r}, base_url=<configured>, "
            "api_key=<configured>, model=<configured>)"
        )


def _host_header(hostname: str, port: int) -> str:
    formatted = f"[{hostname}]" if ":" in hostname else hostname
    return formatted if port == 443 else f"{formatted}:{port}"


class _PinnedHTTPSAdapter(HTTPAdapter):
    """HTTPS adapter connecting only to one validated IP."""

    def __init__(self, pinned_ip: str, hostname: str, port: int):
        super().__init__(max_retries=0)
        self._hostname = hostname
        self._port = port
        self._pinned_pool = HTTPSConnectionPool(
            host=pinned_ip,
            port=port,
            maxsize=1,
            block=True,
            assert_hostname=hostname,
            server_hostname=hostname,
        )

    def get_connection_with_tls_context(
        self,
        request,
        verify,
        proxies=None,
        cert=None,
    ):
        del request, verify, cert
        if proxies:
            raise requests.exceptions.ProxyError("proxies are disabled")
        return self._pinned_pool

    def add_headers(self, request, **kwargs):
        super().add_headers(request, **kwargs)
        request.headers["Host"] = _host_header(self._hostname, self._port)

    def close(self):
        self._pinned_pool.close()
        super().close()


def _is_public_address(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        value.is_global
        and not value.is_private
        and not value.is_loopback
        and not value.is_link_local
        and not value.is_reserved
        and not value.is_multicast
        and not value.is_unspecified
    )


def _required_string(value: object, *, maximum: int = 4_096) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid model configuration")
    return value.strip()


def _safe_base_url(value: object) -> str:
    base_url = _required_string(value, maximum=2_048).rstrip("/")
    try:
        parsed = urlsplit(base_url)
        # Reading port also rejects malformed or out-of-range values.
        parsed.port
    except ValueError:
        raise ValueError("invalid model configuration") from None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid model configuration")
    hostname = parsed.hostname.casefold().rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("invalid model configuration") from None
    if (
        not hostname
        or any(character.isspace() for character in hostname)
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or "%" in hostname
    ):
        raise ValueError("invalid model configuration")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not _is_public_address(literal):
        raise ValueError("invalid model configuration")
    return base_url


def _normalize_selected_model(item: dict) -> ModelConfig:
    mode = _required_string(item.get("mode")).casefold()
    if mode not in {"responses", "chat"}:
        raise ValueError("invalid model configuration")
    provider_value = item.get("provider", "custom")
    provider = _required_string(provider_value).casefold()
    return ModelConfig(
        id=_required_string(item.get("id")),
        provider=provider,
        enabled=True,
        base_url=_safe_base_url(item.get("base_url")),
        api_key=_required_string(item.get("api_key"), maximum=8_192),
        model=_required_string(item.get("model"), maximum=512),
        mode=mode,
    )


def _validate_model_config(config: object) -> ModelConfig:
    if not isinstance(config, ModelConfig) or config.enabled is not True:
        raise ValueError("invalid model configuration")
    if not isinstance(config.mode, str) or config.mode.strip() not in {
        "responses",
        "chat",
    }:
        raise ValueError("invalid model configuration")
    return _normalize_selected_model(
        {
            "id": config.id,
            "provider": config.provider,
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
            "mode": config.mode,
        }
    )


def select_model(settings: object, model_id: object = "") -> ModelConfig:
    """Select an enabled explicit/default model, or first enabled when no ID exists."""

    if not isinstance(settings, dict):
        raise ValueError("invalid model settings")
    models = settings.get("models")
    if not isinstance(models, dict):
        raise ValueError("invalid model settings")
    items = models.get("items")
    if not isinstance(items, list):
        raise ValueError("invalid model settings")

    indexed: dict[str, dict] = {}
    ordered: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid model settings")
        item_id = _required_string(item.get("id"))
        if item_id in indexed:
            raise ValueError("duplicate model id")
        indexed[item_id] = item
        ordered.append(item)

    if model_id is None:
        model_id = ""
    if not isinstance(model_id, str):
        raise ValueError("invalid model id")
    explicit_id = model_id.strip()

    default_value = models.get("default_model_id", "")
    if default_value is None:
        default_value = ""
    if not isinstance(default_value, str):
        raise ValueError("invalid default model id")
    target_id = explicit_id or default_value.strip()

    if target_id:
        selected = indexed.get(target_id)
        if selected is None:
            raise ValueError("configured model was not found")
        if selected.get("enabled", True) is not True:
            raise ValueError("configured model is disabled")
    else:
        selected = next(
            (item for item in ordered if item.get("enabled", True) is True),
            None,
        )
        if selected is None:
            raise ValueError("no enabled model is configured")

    return _normalize_selected_model(selected)


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("invalid UTF-8 text") from None


def _validate_messages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_MESSAGES:
        raise ValueError("invalid messages")
    result: list[dict[str, str]] = []
    total_bytes = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError("invalid messages")
        role = item["role"]
        content = item["content"]
        if (
            not isinstance(role, str)
            or role not in _MESSAGE_ROLES
            or not isinstance(content, str)
        ):
            raise ValueError("invalid messages")
        content_bytes = _utf8_size(content)
        if content_bytes > _MAX_MESSAGE_BYTES:
            raise ValueError("invalid messages")
        total_bytes += content_bytes + len(role)
        if total_bytes > _MAX_MESSAGES_BYTES:
            raise ValueError("invalid messages")
        result.append({"role": role, "content": content})
    return result


def _json_value_is_safe(
    value: object,
    *,
    maximum_depth: int,
    maximum_nodes: int,
) -> bool:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > maximum_depth or nodes > maximum_nodes:
            return False
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeError:
                return False
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                return False
            continue
        if isinstance(current, dict):
            for key in current:
                if not isinstance(key, str):
                    return False
                try:
                    key.encode("utf-8")
                except UnicodeError:
                    return False
            stack.extend((item, depth + 1) for item in current.values())
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        return False
    return True


def _validate_schema(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or value.get("type") != "object"
        or not _json_value_is_safe(
            value,
            maximum_depth=_MAX_JSON_DEPTH,
            maximum_nodes=_MAX_SCHEMA_NODES,
        )
    ):
        raise ValueError("invalid schema")
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid schema") from None
    if len(encoded) > _MAX_SCHEMA_BYTES:
        raise ValueError("invalid schema")
    return value


def _verify_public_resolution(
    config: ModelConfig,
    resolver: Callable,
) -> tuple[str, str, int]:
    parsed = urlsplit(config.base_url)
    raw_hostname = parsed.hostname
    port = parsed.port or 443
    if raw_hostname is None:
        raise ModelRequestError("model_invalid_request")
    try:
        hostname = raw_hostname.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        raise ModelRequestError("model_invalid_request") from None
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except TimeoutError:
        raise ModelRequestError("model_timeout") from None
    except Exception:
        raise ModelRequestError("model_network_error") from None
    if not isinstance(records, (list, tuple)) or not records:
        raise ModelRequestError("model_network_error")
    pinned_ip = ""
    for record in records:
        try:
            address = record[4][0]
            resolved = ipaddress.ip_address(address)
        except (IndexError, TypeError, ValueError):
            raise ModelRequestError("model_network_error") from None
        if not _is_public_address(resolved):
            raise ModelRequestError("model_network_error")
        if not pinned_ip:
            pinned_ip = str(resolved)
    return pinned_ip, hostname, port


def _schema_format(schema: dict) -> dict:
    return {
        "type": "json_schema",
        "name": _STRUCTURED_OUTPUT_NAME,
        "schema": schema,
        "strict": True,
    }


def _append_bounded_text(
    parts: list[str],
    value: str,
    current_bytes: int,
) -> int:
    try:
        total_bytes = current_bytes + _utf8_size(value) + bool(parts)
    except ValueError:
        raise ModelRequestError("model_invalid_response") from None
    if total_bytes > _MAX_OUTPUT_BYTES:
        raise ModelRequestError("model_output_too_large")
    parts.append(value)
    return total_bytes


def _extract_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ModelRequestError("model_invalid_response")

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        try:
            output_size = _utf8_size(output_text)
        except ValueError:
            raise ModelRequestError("model_invalid_response") from None
        if output_size > _MAX_OUTPUT_BYTES:
            raise ModelRequestError("model_output_too_large")
        return output_text

    texts: list[str] = []
    text_bytes = 0
    output = payload.get("output")
    if isinstance(output, list):
        if len(output) > _MAX_COLLECTION_ITEMS:
            raise ModelRequestError("model_invalid_response")
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            if len(content) > _MAX_COLLECTION_ITEMS:
                raise ModelRequestError("model_invalid_response")
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_bytes = _append_bounded_text(
                        texts,
                        part["text"],
                        text_bytes,
                    )
    if texts:
        return "\n".join(texts)

    choices = payload.get("choices")
    if isinstance(choices, list):
        if len(choices) > _MAX_COLLECTION_ITEMS:
            raise ModelRequestError("model_invalid_response")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content:
                try:
                    content_size = _utf8_size(content)
                except ValueError:
                    raise ModelRequestError("model_invalid_response") from None
                if content_size > _MAX_OUTPUT_BYTES:
                    raise ModelRequestError("model_output_too_large")
                return content
            if isinstance(content, list):
                if len(content) > _MAX_COLLECTION_ITEMS:
                    raise ModelRequestError("model_invalid_response")
                texts = []
                text_bytes = 0
                for part in content:
                    if (
                        isinstance(part, dict)
                        and isinstance(part.get("text"), str)
                    ):
                        text_bytes = _append_bounded_text(
                            texts,
                            part["text"],
                            text_bytes,
                        )
                if texts:
                    return "\n".join(texts)

    raise ModelRequestError("model_invalid_response")


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError()


def _parse_json_object(text: str, *, status: int | None) -> dict:
    try:
        text_bytes = _utf8_size(text)
    except ValueError:
        raise ModelRequestError("model_invalid_json", status) from None
    if text_bytes > _MAX_OUTPUT_BYTES:
        raise ModelRequestError("model_output_too_large", status)
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        raise ModelRequestError("model_invalid_json", status) from None
    if not isinstance(parsed, dict) or not _json_value_is_safe(
        parsed,
        maximum_depth=_MAX_JSON_DEPTH,
        maximum_nodes=_MAX_JSON_NODES,
    ):
        raise ModelRequestError("model_invalid_json", status)
    return parsed


def _response_content_length(response: object) -> int | None:
    headers = getattr(response, "headers", {})
    if not hasattr(headers, "get"):
        raise ModelRequestError("model_invalid_response")
    value = headers.get("Content-Length")
    if value is None:
        value = headers.get("content-length")
    if value is None or value == "":
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        raise ModelRequestError("model_invalid_response") from None
    if length < 0:
        raise ModelRequestError("model_invalid_response")
    return length


def _read_response_bytes(response: object) -> bytes:
    content_length = _response_content_length(response)
    if content_length is not None and content_length > _MAX_RESPONSE_BYTES:
        raise ModelRequestError("model_output_too_large")

    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        result = bytearray()
        for chunk in iterator(chunk_size=65_536):
            if chunk is None:
                continue
            if isinstance(chunk, str):
                try:
                    chunk = chunk.encode("utf-8")
                except UnicodeError:
                    raise ModelRequestError("model_invalid_response") from None
            if not isinstance(chunk, (bytes, bytearray)):
                raise ModelRequestError("model_invalid_response")
            result.extend(chunk)
            if len(result) > _MAX_RESPONSE_BYTES:
                raise ModelRequestError("model_output_too_large")
        return bytes(result)

    content = getattr(response, "content", None)
    if content is not None:
        if isinstance(content, str):
            try:
                content = content.encode("utf-8")
            except UnicodeError:
                raise ModelRequestError("model_invalid_response") from None
        if not isinstance(content, (bytes, bytearray)):
            raise ModelRequestError("model_invalid_response")
        if len(content) > _MAX_RESPONSE_BYTES:
            raise ModelRequestError("model_output_too_large")
        return bytes(content)

    text = getattr(response, "text", None)
    if not isinstance(text, str):
        raise ModelRequestError("model_invalid_response")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise ModelRequestError("model_invalid_response") from None
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ModelRequestError("model_output_too_large")
    return encoded


def _parse_response_payload(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ModelRequestError("model_invalid_response") from None
    if not isinstance(payload, dict) or not _json_value_is_safe(
        payload,
        maximum_depth=_MAX_JSON_DEPTH,
        maximum_nodes=_MAX_JSON_NODES,
    ):
        raise ModelRequestError("model_invalid_response")
    return payload


def ask_model_json(
    config: ModelConfig,
    messages: object,
    schema: object,
    *,
    request_fn: Callable = _DEFAULT_REQUEST_FN,
    resolver: Callable = socket.getaddrinfo,
) -> dict:
    """Call configured model and return one strict JSON object."""

    try:
        config = _validate_model_config(config)
        messages = _validate_messages(messages)
        schema = _validate_schema(schema)
    except ValueError:
        raise ModelRequestError("model_invalid_request")

    schema_format = _schema_format(schema)
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    if config.mode == "responses":
        url = f"{config.base_url}/responses"
        body = {
            "model": config.model,
            "input": messages,
            "store": False,
            "text": {"format": schema_format},
        }
    else:
        url = f"{config.base_url}/chat/completions"
        body = {
            "model": config.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_format["name"],
                    "schema": schema_format["schema"],
                    "strict": schema_format["strict"],
                },
            },
        }

    pinned_ip, hostname, port = _verify_public_resolution(config, resolver)

    response = None
    transport_session = None
    try:
        request_kwargs = {
            "json": body,
            "headers": headers,
            "timeout": 90,
            "allow_redirects": False,
            "stream": True,
        }
        if request_fn is _DEFAULT_REQUEST_FN:
            transport_session = requests.Session()
            transport_session.trust_env = False
            transport_session.mount(
                "https://",
                _PinnedHTTPSAdapter(pinned_ip, hostname, port),
            )
            response = transport_session.post(
                url,
                proxies={},
                verify=True,
                **request_kwargs,
            )
        else:
            response = request_fn(url, **request_kwargs)
    except (requests.Timeout, TimeoutError):
        if transport_session is not None:
            try:
                transport_session.close()
            except Exception:
                pass
        raise ModelRequestError("model_timeout") from None
    except Exception:
        if transport_session is not None:
            try:
                transport_session.close()
            except Exception:
                pass
        raise ModelRequestError("model_network_error") from None

    try:
        try:
            raw_status = getattr(response, "status_code", None)
        except Exception:
            raise ModelRequestError("model_invalid_response") from None
        status = raw_status if type(raw_status) is int else None
        if status is None:
            raise ModelRequestError("model_invalid_response")
        if status < 200 or status >= 300:
            raise ModelRequestError("model_http_error", status)

        try:
            raw_payload = _read_response_bytes(response)
        except (requests.Timeout, TimeoutError):
            raise ModelRequestError("model_timeout", status) from None
        except ModelRequestError as error:
            raise ModelRequestError(error.code, status) from None
        except Exception:
            raise ModelRequestError("model_network_error", status) from None

        try:
            payload = _parse_response_payload(raw_payload)
            text = _extract_text(payload)
        except ModelRequestError as error:
            raise ModelRequestError(error.code, status) from None
        return _parse_json_object(text, status=status)
    finally:
        try:
            close = getattr(response, "close", None)
        except Exception:
            close = None
        if callable(close):
            try:
                close()
            except Exception:
                pass
        if transport_session is not None:
            try:
                transport_session.close()
            except Exception:
                pass


__all__ = [
    "ModelConfig",
    "ModelRequestError",
    "ask_model_json",
    "select_model",
]
