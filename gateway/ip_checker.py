import requests

from gateway.settings_store import load_settings

DEFAULT_IPINFO_URL = "https://ipinfo.io/json"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10


def fetch_ip_info(proxy_url: str) -> dict:
    settings = load_settings()
    response = requests.get(
        settings["services"].get("ipinfo_url") or DEFAULT_IPINFO_URL,
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=settings["timeouts"].get(
            "ip_check_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
    )
    response.raise_for_status()
    return response.json()
