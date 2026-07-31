from gateway.proxy_pool import select_proxy_from_pool
from gateway.settings_store import load_settings


def build_static_proxy_url(proxy: dict, protocol: str = "socks5") -> str:
    scheme = "socks5h" if protocol == "socks5" else "http"
    return (
        f"{scheme}://{proxy['username']}:{proxy['password']}@"
        f"{proxy['host']}:{proxy['port']}"
    )


def generate_proxy_url(account_id) -> str:
    settings = load_settings()
    proxy_pool = settings.get("proxy_pool", {})
    selected_proxy = select_proxy_from_pool(proxy_pool.get("items", []), account_id)
    if selected_proxy:
        return build_static_proxy_url(
            selected_proxy,
            proxy_pool.get("protocol", "socks5"),
        )

    config = settings["proxy"]
    session_username = f"{config['username']}-zone-custom-session-{account_id}"

    return (
        f"http://{session_username}:{config['password']}@"
        f"{config['host']}:{config['port']}"
    )
