import hashlib


def parse_proxy_pool(raw_text: str | None) -> list[dict[str, str]]:
    items = []

    for line_number, raw_line in enumerate(str(raw_text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        parts = [part.strip() for part in line.split(":")]
        if len(parts) != 4 or any(not part for part in parts):
            raise ValueError(
                f"Invalid proxy pool line {line_number}: expected host:port:username:password"
            )

        host, port, username, password = parts
        items.append(
            {
                "host": host,
                "port": port,
                "username": username,
                "password": password,
            }
        )

    return items


def format_proxy_pool(items: list[dict[str, str]] | None) -> str:
    lines = []
    for item in items or []:
        lines.append(
            f"{item.get('host', '')}:{item.get('port', '')}:"
            f"{item.get('username', '')}:{item.get('password', '')}"
        )
    return "\n".join(lines)


def proxy_pool_key(item: dict[str, str]) -> str:
    return (
        f"{item.get('host', '')}:{item.get('port', '')}:"
        f"{item.get('username', '')}:{item.get('password', '')}"
    )


def summarize_proxy_pool(
    items: list[dict[str, str]] | None,
    assigned_sessions: list[str] | set[str] | tuple[str, ...] | None,
    *,
    page: int = 1,
    page_size: int | None = None,
    search: str = "",
) -> dict:
    pool_items = items or []
    assigned_set = {session for session in assigned_sessions or [] if session}
    summarized_items = []
    assigned_count = 0
    search_text = str(search or "").strip().lower()

    for item in pool_items:
        key = proxy_pool_key(item)
        assigned = key in assigned_set
        if assigned:
            assigned_count += 1
        summarized_items.append(
            {
                "host": item.get("host", ""),
                "port": item.get("port", ""),
                "username": item.get("username", ""),
                "assigned": assigned,
            }
        )

    filtered_items = [
        item for item in summarized_items
        if not search_text
        or search_text in item["host"].lower()
        or search_text in item["port"].lower()
        or search_text in item["username"].lower()
    ]
    if page_size is None:
        page_size = len(filtered_items) or 1
    page_size = max(int(page_size or 1), 1)
    page = max(int(page or 1), 1)
    filtered_total = len(filtered_items)
    page_count = max((filtered_total + page_size - 1) // page_size, 1)
    page = min(page, page_count)
    start = (page - 1) * page_size
    visible_items = filtered_items[start:start + page_size]

    total = len(pool_items)
    return {
        "total": total,
        "assigned": assigned_count,
        "remaining": max(total - assigned_count, 0),
        "filtered_total": filtered_total,
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "items": visible_items,
    }


def select_proxy_from_pool(
    items: list[dict[str, str]] | None,
    account_id,
) -> dict[str, str] | None:
    if not items:
        return None

    digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(items)
    return items[index]
