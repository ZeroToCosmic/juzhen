from dataclasses import dataclass

from gateway.proxy_pool import select_proxy_from_pool
from gateway.settings_store import load_settings


@dataclass(frozen=True)
class ProxyConfig:
    host: str | None
    port: str | None
    username: str | None
    password: str | None


def load_proxy_config(account_id=None) -> ProxyConfig:
    settings = load_settings()
    selected_proxy = select_proxy_from_pool(
        settings.get("proxy_pool", {}).get("items", []),
        account_id,
    )
    proxy = selected_proxy or settings["proxy"]

    return ProxyConfig(
        host=proxy["host"],
        port=proxy["port"],
        username=proxy["username"],
        password=proxy["password"],
    )
