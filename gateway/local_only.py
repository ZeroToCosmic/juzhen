"""Fail-closed network guard for the local direct V2 console."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from flask import jsonify, request


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def install_local_only_guard(app) -> None:
    """Permit only loopback clients using this process's local Host header."""

    @app.before_request
    def local_only_guard():
        if _is_loopback_address(request.remote_addr) and _is_local_host(
            request.environ.get("HTTP_HOST", ""), app.config["SERVER_PORT"]
        ):
            return None
        if request.path.startswith("/api/"):
            return jsonify(
                {"error": {"code": "local_access_only", "message": "仅允许本机访问。"}}
            ), 403
        return "Local access only", 403, {"Content-Type": "text/plain; charset=utf-8"}


def _is_loopback_address(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _is_local_host(host_header: object, server_port: object) -> bool:
    """Reject malformed/foreign Host headers; never trust forwarding headers."""

    if not isinstance(host_header, str) or not host_header or any(
        character.isspace() for character in host_header
    ):
        return False
    if any(character in host_header for character in "@,/?#"):
        return False
    try:
        parsed = urlsplit(f"//{host_header}")
        hostname = parsed.hostname
        port = parsed.port
        expected_port = int(server_port)
    except (TypeError, ValueError):
        return False
    if hostname is None or hostname.casefold() not in _LOOPBACK_HOSTS:
        return False
    return port is None or port == expected_port


__all__ = ["install_local_only_guard"]
