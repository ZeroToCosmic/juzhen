from gateway.proxy_pool import (
    format_proxy_pool,
    parse_proxy_pool,
    select_proxy_from_pool,
    summarize_proxy_pool,
)


def test_parse_proxy_pool_accepts_one_proxy_per_line():
    items = parse_proxy_pool(
        """
        192.53.69.143:6781:nsucssou:3mjeb2p392yk
        203.0.113.8:9000:user2:pass2
        """
    )

    assert items == [
        {
            "host": "192.53.69.143",
            "port": "6781",
            "username": "nsucssou",
            "password": "3mjeb2p392yk",
        },
        {
            "host": "203.0.113.8",
            "port": "9000",
            "username": "user2",
            "password": "pass2",
        },
    ]


def test_parse_proxy_pool_rejects_invalid_line():
    try:
        parse_proxy_pool("192.53.69.143:6781:only-user")
    except ValueError as error:
        assert "line 1" in str(error)
        assert "host:port:username:password" in str(error)
    else:
        raise AssertionError("Expected invalid proxy pool line to raise ValueError")


def test_select_proxy_from_pool_is_stable_for_same_account():
    items = parse_proxy_pool(
        """
        192.53.69.143:6781:nsucssou:3mjeb2p392yk
        203.0.113.8:9000:user2:pass2
        """
    )

    first = select_proxy_from_pool(items, "account-123")
    second = select_proxy_from_pool(items, "account-123")

    assert first == second
    assert first in items


def test_format_proxy_pool_round_trips_items_to_text():
    items = parse_proxy_pool("192.53.69.143:6781:nsucssou:3mjeb2p392yk")

    assert format_proxy_pool(items) == "192.53.69.143:6781:nsucssou:3mjeb2p392yk"


def test_summarize_proxy_pool_counts_total_assigned_and_remaining():
    items = parse_proxy_pool(
        """
        192.53.69.143:6781:nsucssou:3mjeb2p392yk
        203.0.113.8:9000:user2:pass2
        """
    )

    summary = summarize_proxy_pool(
        items,
        [
            "192.53.69.143:6781:nsucssou:3mjeb2p392yk",
            "192.53.69.143:6781:nsucssou:3mjeb2p392yk",
            "198.51.100.5:7000:unused:unused",
        ],
    )

    assert summary["total"] == 2
    assert summary["assigned"] == 1
    assert summary["remaining"] == 1
    assert summary["items"][0]["assigned"] is True
    assert summary["items"][1]["assigned"] is False


def test_summarize_proxy_pool_paginates_and_filters_visible_items():
    items = [
        {"host": f"203.0.113.{index}", "port": "9000", "username": f"user{index}", "password": "pass"}
        for index in range(1, 121)
    ]

    summary = summarize_proxy_pool(
        items,
        [],
        page=2,
        page_size=25,
        search="user",
    )

    assert summary["total"] == 120
    assert summary["filtered_total"] == 120
    assert summary["page"] == 2
    assert summary["page_size"] == 25
    assert summary["page_count"] == 5
    assert len(summary["items"]) == 25
    assert summary["items"][0]["username"] == "user26"
