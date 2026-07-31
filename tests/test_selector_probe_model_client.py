import json
from dataclasses import replace
import socket

import pytest
import requests

import selector_probe.model_client as model_client
from selector_probe.model_client import (
    ModelRequestError,
    ask_model_json,
    select_model,
)


SCHEMA = {
    "type": "object",
    "properties": {"locators": {"type": "array"}},
    "required": ["locators"],
    "additionalProperties": False,
}


def _model(
    model_id="gpt-main",
    *,
    enabled=True,
    mode="responses",
    base_url="https://api.example.test/v1",
):
    return {
        "id": model_id,
        "provider": "custom",
        "enabled": enabled,
        "base_url": base_url,
        "api_key": "fake-secret-key",
        "model": "test-model",
        "mode": mode,
    }


def _settings(*items, default_model_id="gpt-main"):
    return {
        "models": {
            "default_model_id": default_model_id,
            "items": list(items or (_model(),)),
        }
    }


def _global_resolver(_host, port, **_kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", port),
        )
    ]


def _ask_model_json(*args, **kwargs):
    kwargs.setdefault("resolver", _global_resolver)
    return ask_model_json(*args, **kwargs)


class _Response:
    def __init__(
        self,
        payload,
        *,
        status_code=200,
        text=None,
        headers=None,
        chunks=None,
    ):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        if text is None:
            text = "{malformed" if isinstance(payload, Exception) else json.dumps(payload)
        self.text = text
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    def json(self):
        raise AssertionError("streaming client must not call response.json()")

    def iter_content(self, chunk_size=1):
        del chunk_size
        chunks = self._chunks
        if chunks is None:
            chunks = [self.text.encode()]
        yield from chunks

    def close(self):
        self.closed = True


def test_responses_request_disables_storage_and_requires_json_schema():
    captured = {}

    def request_fn(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _Response({"output_text": '{"locators":[]}'})

    result = _ask_model_json(
        select_model(_settings(), ""),
        [{"role": "user", "content": "data"}],
        SCHEMA,
        request_fn=request_fn,
    )

    assert result == {"locators": []}
    assert captured["url"] == "https://api.example.test/v1/responses"
    assert captured["timeout"] == 90
    assert captured["allow_redirects"] is False
    assert captured["stream"] is True
    assert captured["headers"] == {
        "Authorization": "Bearer fake-secret-key",
        "Content-Type": "application/json",
    }
    assert captured["json"]["model"] == "test-model"
    assert captured["json"]["input"] == [{"role": "user", "content": "data"}]
    assert captured["json"]["store"] is False
    assert captured["json"]["text"]["format"] == {
        "type": "json_schema",
        "name": "selector_probe",
        "schema": SCHEMA,
        "strict": True,
    }


def test_chat_request_uses_json_schema_response_format():
    captured = {}

    def request_fn(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _Response(
            {"choices": [{"message": {"content": '{"locators":[]}'}}]}
        )

    result = _ask_model_json(
        select_model(_settings(_model(mode="chat")), ""),
        [{"role": "system", "content": "JSON only"}],
        SCHEMA,
        request_fn=request_fn,
    )

    assert result == {"locators": []}
    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "JSON only"}
    ]
    assert captured["json"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "selector_probe",
            "schema": SCHEMA,
            "strict": True,
        },
    }


def test_select_model_prefers_explicit_then_default_and_falls_back_without_default():
    first = _model("first", mode="chat")
    second = _model("second")
    settings = _settings(first, second, default_model_id="second")

    assert select_model(settings, "first").id == "first"
    assert select_model(settings, "").id == "second"
    assert select_model(_settings(first, second, default_model_id=""), "").id == "first"


@pytest.mark.parametrize(
    "settings,model_id",
    [
        (_settings(_model(enabled=False)), ""),
        (_settings(_model("one"), _model("one")), ""),
        (_settings(_model("one"), default_model_id="missing"), ""),
        (_settings(_model("one"), default_model_id="one"), "missing"),
    ],
)
def test_select_model_rejects_disabled_duplicate_or_missing_target(settings, model_id):
    with pytest.raises(ValueError):
        select_model(settings, model_id)


@pytest.mark.parametrize(
    "updates",
    [
        {"base_url": "http://api.example.test/v1"},
        {"base_url": "https://user:pass@api.example.test/v1"},
        {"base_url": "https://api.example.test/v1?key=secret"},
        {"base_url": "https://api.example.test/v1#fragment"},
        {"api_key": ""},
        {"model": ""},
        {"mode": "legacy"},
    ],
)
def test_select_model_rejects_unsafe_or_incomplete_selected_model(updates):
    item = _model()
    item.update(updates)

    with pytest.raises(ValueError) as caught:
        select_model(_settings(item), "")

    message = f"{caught.value!s} {caught.value!r}"
    assert "fake-secret-key" not in message
    assert "api.example.test" not in message


@pytest.mark.parametrize(
    "base_url",
    [
        "https://localhost/v1",
        "https://service.local/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.1/v1",
        "https://169.254.1.1/v1",
        "https://224.0.0.1/v1",
        "https://[::1]/v1",
        "https://[fc00::1]/v1",
        "https://[fe80::1]/v1",
    ],
)
def test_select_model_rejects_non_public_literal_or_local_base_urls(base_url):
    with pytest.raises(ValueError):
        select_model(_settings(_model(base_url=base_url)), "")


def test_dns_resolution_rejects_any_non_global_address_before_request():
    requested = False

    def resolver(_host, port, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", port),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.5", port),
            ),
        ]

    def request_fn(_url, **_kwargs):
        nonlocal requested
        requested = True
        return _Response({"output_text": "{}"})

    with pytest.raises(ModelRequestError) as caught:
        ask_model_json(
            select_model(_settings(), ""),
            [{"role": "user", "content": "safe"}],
            SCHEMA,
            request_fn=request_fn,
            resolver=resolver,
        )

    assert caught.value.code == "model_network_error"
    assert requested is False


@pytest.mark.parametrize(
    "resolved_ip",
    [
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    ],
)
def test_default_transport_pins_validated_ip_and_preserves_tls_identity(
    monkeypatch,
    resolved_ip,
):
    captured = {}
    resolver_calls = []

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.closed = False
            captured["session"] = self

        def mount(self, prefix, adapter):
            captured.update(prefix=prefix, adapter=adapter)

        def post(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return _Response({"output_text": '{"locators":[]}'})

        def close(self):
            self.closed = True

    def resolver(host, port, **_kwargs):
        resolver_calls.append((host, port))
        family = socket.AF_INET6 if ":" in resolved_ip else socket.AF_INET
        address = (resolved_ip, port, 0, 0) if family == socket.AF_INET6 else (
            resolved_ip,
            port,
        )
        return [
            (
                family,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                address,
            )
        ]

    monkeypatch.setattr(model_client.requests, "Session", FakeSession)

    result = ask_model_json(
        select_model(_settings(), ""),
        [{"role": "user", "content": "safe"}],
        SCHEMA,
        resolver=resolver,
    )

    adapter = captured["adapter"]
    pool = adapter._pinned_pool
    assert result == {"locators": []}
    assert resolver_calls == [("api.example.test", 443)]
    assert captured["prefix"] == "https://"
    assert captured["url"] == "https://api.example.test/v1/responses"
    assert captured["session"].trust_env is False
    assert captured["session"].closed is True
    assert captured["kwargs"]["proxies"] == {}
    assert captured["kwargs"]["verify"] is True
    assert pool.host == resolved_ip
    assert pool.assert_hostname == "api.example.test"
    assert pool.conn_kw["server_hostname"] == "api.example.test"
    adapter.cert_verify(
        pool,
        "https://api.example.test/v1/responses",
        True,
        None,
    )
    connection = pool._new_conn()
    assert pool.cert_reqs == "CERT_REQUIRED"
    assert connection.host == resolved_ip
    assert connection.assert_hostname == "api.example.test"
    assert connection.server_hostname == "api.example.test"

    prepared = requests.Request(
        "POST",
        "https://api.example.test/v1/responses",
    ).prepare()
    adapter.add_headers(prepared)
    assert prepared.headers["Host"] == "api.example.test"


def test_pinned_ipv6_literal_uses_raw_pool_ip_and_bracketed_host_header():
    address = "2606:2800:220:1:248:1893:25c8:1946"
    adapter = model_client._PinnedHTTPSAdapter(address, address, 443)
    prepared = requests.Request(
        "POST",
        f"https://[{address}]/v1/responses",
    ).prepare()

    adapter.add_headers(prepared)

    assert adapter._pinned_pool.host == address
    assert adapter._pinned_pool.assert_hostname == address
    assert adapter._pinned_pool.conn_kw["server_hostname"] == address
    assert prepared.headers["Host"] == f"[{address}]"


@pytest.mark.parametrize(
    "update",
    [
        {"enabled": False},
        {"id": "bad\nid"},
        {"provider": "bad\rprovider"},
        {"base_url": "http://api.example.test/v1"},
        {"api_key": "secret\ninjected"},
        {"model": "bad\x00model"},
        {"mode": "legacy"},
    ],
)
def test_ask_model_json_revalidates_direct_model_config(update):
    config = replace(select_model(_settings(), ""), **update)
    requested = False

    def request_fn(_url, **_kwargs):
        nonlocal requested
        requested = True
        return _Response({"output_text": "{}"})

    with pytest.raises(ModelRequestError) as caught:
        _ask_model_json(
            config,
            [{"role": "user", "content": "safe"}],
            SCHEMA,
            request_fn=request_fn,
        )

    assert caught.value.code == "model_invalid_request"
    assert requested is False


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "tool", "content": "safe"}],
        [{"role": "user", "content": "safe", "extra": True}],
        [{"role": "user", "content": ["not", "text"]}],
        [{"role": "user", "content": "safe"}] * 65,
        [{"role": "user", "content": "x" * 131_073}],
        [{"role": "user", "content": "x" * 120_000}] * 5,
    ],
)
def test_messages_require_exact_bounded_shape(messages):
    with pytest.raises(ModelRequestError) as caught:
        _ask_model_json(
            select_model(_settings(), ""),
            messages,
            SCHEMA,
            request_fn=lambda _url, **_kwargs: _Response({"output_text": "{}"}),
        )
    assert caught.value.code == "model_invalid_request"


@pytest.mark.parametrize(
    "schema",
    [
        [],
        {"type": "array"},
        {"type": "object", "minimum": float("inf")},
        {"type": "object", "description": "x" * 262_145},
    ],
)
def test_schema_requires_bounded_json_safe_object_root(schema):
    with pytest.raises(ModelRequestError) as caught:
        _ask_model_json(
            select_model(_settings(), ""),
            [{"role": "user", "content": "safe"}],
            schema,
            request_fn=lambda _url, **_kwargs: _Response({"output_text": "{}"}),
        )
    assert caught.value.code == "model_invalid_request"


def test_schema_rejects_excessive_depth_and_node_count():
    deep_schema = {"type": "object"}
    cursor = deep_schema
    for _ in range(33):
        child = {}
        cursor["child"] = child
        cursor = child
    wide_schema = {
        "type": "object",
        "properties": {str(index): {} for index in range(10_001)},
    }

    for schema in (deep_schema, wide_schema):
        with pytest.raises(ModelRequestError) as caught:
            _ask_model_json(
                select_model(_settings(), ""),
                [{"role": "user", "content": "safe"}],
                schema,
                request_fn=lambda _url, **_kwargs: _Response(
                    {"output_text": "{}"}
                ),
            )
        assert caught.value.code == "model_invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        {"output_text": '{"locators":[]}'},
        {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": '{"locators":[]}'}
                    ]
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": '{"locators":[]}'}
                        ]
                    }
                }
            ]
        },
    ],
)
def test_supported_response_shapes_are_parsed(payload):
    result = _ask_model_json(
        select_model(_settings(), ""),
        [{"role": "user", "content": "safe"}],
        SCHEMA,
        request_fn=lambda _url, **_kwargs: _Response(payload),
    )
    assert result == {"locators": []}


@pytest.mark.parametrize(
    "output",
    [
        "```json\n{}\n```",
        "Result: {}",
        '{"value": 1, "value": 2}',
        "[]",
        '{"value": NaN}',
        '{"value": 1e999}',
        "null",
    ],
)
def test_model_output_must_be_one_strict_json_object(output):
    with pytest.raises(ModelRequestError) as caught:
        _ask_model_json(
            select_model(_settings(), ""),
            [{"role": "user", "content": "safe"}],
            SCHEMA,
            request_fn=lambda _url, **_kwargs: _Response(
                {"output_text": output}
            ),
        )

    assert caught.value.code == "model_invalid_json"


def test_model_output_rejects_oversize_and_excessive_depth():
    outputs = [
        '{"value":"' + ("x" * 1_048_576) + '"}',
        json.dumps({"value": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]}),
    ]

    for output in outputs:
        with pytest.raises(ModelRequestError) as caught:
            _ask_model_json(
                select_model(_settings(), ""),
                [{"role": "user", "content": "safe"}],
                SCHEMA,
                request_fn=lambda _url, output=output, **_kwargs: _Response(
                    {"output_text": output}
                ),
            )
        assert caught.value.code in {
            "model_output_too_large",
            "model_invalid_json",
        }


@pytest.mark.parametrize(
    "response_or_error,code,status",
    [
        (requests.Timeout("page text"), "model_timeout", None),
        (requests.ConnectionError("https://secret.example/path"), "model_network_error", None),
        (RuntimeError("secret request error"), "model_network_error", None),
        (_Response({}, status_code=503, text="secret body"), "model_http_error", 503),
        (_Response({}, status_code=302, text="redirect"), "model_http_error", 302),
        (_Response(ValueError("secret body")), "model_invalid_response", 200),
        (_Response({"output_text": "not-json"}), "model_invalid_json", 200),
        (_Response({"unexpected": "secret output"}), "model_invalid_response", 200),
    ],
)
def test_model_failures_expose_safe_codes_only(response_or_error, code, status):
    def request_fn(_url, **_kwargs):
        if isinstance(response_or_error, Exception):
            raise response_or_error
        return response_or_error

    with pytest.raises(ModelRequestError) as caught:
        _ask_model_json(
            select_model(_settings(), ""),
            [{"role": "user", "content": "private page content"}],
            SCHEMA,
            request_fn=request_fn,
        )

    assert caught.value.code == code
    assert caught.value.status == status
    exposed = f"{caught.value!s} {caught.value!r}"
    for secret in (
        "fake-secret-key",
        "api.example.test",
        "secret body",
        "secret output",
        "private page content",
    ):
        assert secret not in exposed


def test_response_content_length_and_stream_limit_are_enforced_and_closed():
    responses = [
        _Response(
            {},
            headers={"Content-Length": str(2_000_001)},
            chunks=[b"{}"],
        ),
        _Response(
            {},
            chunks=[b"x" * 1_100_000, b"x" * 1_100_000],
        ),
    ]

    for response in responses:
        with pytest.raises(ModelRequestError) as caught:
            _ask_model_json(
                select_model(_settings(), ""),
                [{"role": "user", "content": "safe"}],
                SCHEMA,
                request_fn=lambda _url, response=response, **_kwargs: response,
            )
        assert caught.value.code == "model_output_too_large"
        assert response.closed is True


@pytest.mark.parametrize("attribute", ["content", "text"])
def test_injected_fake_response_content_and_text_fallbacks(attribute):
    class FakeResponse:
        status_code = 200
        headers = {}

        def __init__(self):
            self.closed = False
            value = json.dumps({"output_text": '{"locators":[]}'})
            setattr(self, attribute, value.encode() if attribute == "content" else value)

        def close(self):
            self.closed = True

    response = FakeResponse()
    result = _ask_model_json(
        select_model(_settings(), ""),
        [{"role": "user", "content": "safe"}],
        SCHEMA,
        request_fn=lambda _url, **_kwargs: response,
    )

    assert result == {"locators": []}
    assert response.closed is True
