"""Errors and validation shared by local action publication adapters."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class PublicationActor:
    actor_id: str
    role: str
    authenticated: bool = False


def require_publication_actor(
    actor: object,
    *,
    require_admin: bool = False,
) -> PublicationActor:
    if not isinstance(actor, PublicationActor) or not actor.authenticated:
        raise PublishGateError("authenticated publication actor is required")
    if not actor.actor_id.strip() or len(actor.actor_id.strip()) > 120:
        raise PublishGateError("release actor is required")
    if not actor.role.strip() or len(actor.role.strip()) > 32:
        raise PublishGateError("release actor role is required")
    if require_admin and actor.role != "administrator":
        raise PublishGateError("administrator role is required for waiver")
    return PublicationActor(actor.actor_id.strip(), actor.role.strip(), True)


def is_https_url(value: object) -> bool:
    """Return whether value has a usable HTTPS host and valid optional port."""

    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port < 1)
    ):
        return False

    bracketed = parsed.netloc.startswith("[")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if bracketed or len(hostname) > 253:
            return False
        labels = hostname.split(".")
        return all(_DNS_LABEL.fullmatch(label) for label in labels)
    return not bracketed if address.version == 4 else bracketed


class ActionIdentityError(ValueError):
    """An action identity is missing, tombstoned, or already bound."""


class PublishGateError(ValueError):
    """The exact action content has not satisfied the publication gate."""
