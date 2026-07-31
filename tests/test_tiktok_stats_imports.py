import hashlib
import json
import sqlite3

import pytest

from init_db import init_db
from tiktok_stats.imports import (
    NormalizedUsername,
    existing_account_candidates,
    import_tracked_accounts,
    normalize_tiktok_username,
    parse_username_text,
)
from tiktok_stats.store import StatsStore


def _account_db_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_normalize_tiktok_username_accepts_tokens_mentions_and_exact_profile_urls():
    assert normalize_tiktok_username("  Creator.Name  ") == ("Creator.Name", "creator.name")
    assert normalize_tiktok_username("@Creator_Name") == ("Creator_Name", "creator_name")
    assert normalize_tiktok_username("https://www.tiktok.com/@Creator.Name") == (
        "Creator.Name",
        "creator.name",
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://tiktok.com/@creator",
        "https://www.tiktok.com.evil.example/@creator",
        "https://attacker@www.tiktok.com/@creator",
        "https://www.tiktok.com:443/@creator",
        "https://www.tiktok.com/@creator?lang=en",
        "https://www.tiktok.com/@creator#bio",
        "https://www.tiktok.com/video/123",
        "https://www.tiktok.com/@creator/extra",
        "creator/name",
        "@.creator",
        "@creator.",
    ],
)
def test_normalize_tiktok_username_rejects_ambiguous_or_non_profile_values(value):
    with pytest.raises(ValueError):
        normalize_tiktok_username(value)


def test_parse_username_text_accepts_mixed_newline_csv_and_tsv_values():
    parsed = parse_username_text(
        " Creator , @Second\nhttps://www.tiktok.com/@Third\tFourth.Name\r\n"
    )

    assert [(item.display_name, item.username_key) for item in parsed] == [
        ("Creator", "creator"),
        ("Second", "second"),
        ("Third", "third"),
        ("Fourth.Name", "fourth.name"),
    ]


def test_import_is_per_item_idempotent_and_keeps_valid_values_after_invalid_input(tmp_path):
    store = StatsStore(tmp_path / "stats.db")

    result = import_tracked_accounts(
        store,
        ["Creator", "@creator", "https://www.tiktok.com/@Second", "https://evil.example/@bad"],
        source="manual",
    )

    assert [item.status for item in result.results] == ["added", "existing", "added", "invalid"]
    assert store.count_rows("tracked_accounts") == 2
    rows = store.connection.execute(
        "SELECT username, username_key, sec_uid, status FROM tracked_accounts ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("Creator", "creator", None, "enabled"),
        ("Second", "second", None, "enabled"),
    ]
    store.close()


def test_import_revalidates_normalized_username_objects_at_the_write_boundary(tmp_path):
    store = StatsStore(tmp_path / "stats.db")

    result = import_tracked_accounts(
        store,
        [NormalizedUsername("creator/not-a-token", "forged-key")],
        source="manual",
    )

    assert result.results[0].status == "invalid"
    assert store.count_rows("tracked_accounts") == 0
    store.close()


def test_import_reactivates_disabled_account_without_replacing_row_or_history(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    existing = store.upsert_account("Creator", "creator", source="manual")
    store.disable_account(existing["id"])

    result = import_tracked_accounts(store, ["@CREATOR"], source="manual")

    assert result.results[0].status == "reactivated"
    row = store.connection.execute(
        "SELECT id, status FROM tracked_accounts WHERE username_key = ?", ("creator",)
    ).fetchone()
    assert tuple(row) == (existing["id"], "enabled")
    assert store.count_rows("tracked_accounts") == 1
    store.close()


def test_existing_account_candidates_project_tiktok_channels_without_writing_legacy_db(tmp_path):
    accounts_db_path = tmp_path / "accounts.db"
    init_db(accounts_db_path)
    with sqlite3.connect(accounts_db_path) as connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id, buffer_account_id, proxy_session, status,
                account_name, buffer_channels, buffer_profile_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ads-1",
                "buffer-1",
                "session-1",
                "active",
                "Brand One",
                json.dumps(
                    [
                        {"id": "tt-1", "displayName": "Brand TikTok", "service": "tiktok", "descriptor": "@BrandOne"},
                        {"id": "ig-1", "displayName": "Brand IG", "service": "instagram", "descriptor": "@brandone"},
                    ]
                ),
                json.dumps(["tt-1"]),
            ),
        )
    before = _account_db_digest(accounts_db_path)

    candidates = existing_account_candidates(accounts_db_path, query="brand")

    assert candidates == [
        {
            "source_account_id": "1",
            "account_name": "Brand One",
            "channel_id": "tt-1",
            "channel_name": "Brand TikTok",
            "username": "BrandOne",
            "username_key": "brandone",
        }
    ]
    assert _account_db_digest(accounts_db_path) == before


def test_existing_account_candidates_match_gateway_tiktok_classifier_and_deduplicate(tmp_path):
    accounts_db_path = tmp_path / "accounts.db"
    init_db(accounts_db_path)
    with sqlite3.connect(accounts_db_path) as connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id, buffer_account_id, proxy_session, status,
                account_name, buffer_channels, buffer_profile_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ads-3",
                "buffer-3",
                "session-3",
                "active",
                "Brand Three",
                json.dumps(
                    [
                        {"id": "service-match", "displayName": "Service Match", "service": "tiktok_business", "descriptor": "@BrandThree"},
                        {"id": "descriptor-match", "displayName": "Descriptor Match", "service": "other", "descriptor": "https://www.tiktok.com/@DescriptorOnly"},
                        {"id": "duplicate", "displayName": "Duplicate", "service": "TIKTOK", "descriptor": "@BRANDTHREE"},
                        {"id": "not-tiktok", "displayName": "Not TikTok", "service": "youtube", "descriptor": "@other_channel"},
                    ]
                ),
                json.dumps([]),
            ),
        )

    candidates = existing_account_candidates(accounts_db_path)

    assert [(item["channel_id"], item["username_key"]) for item in candidates] == [
        ("service-match", "brandthree"),
        ("descriptor-match", "descriptoronly"),
    ]


def test_existing_account_candidates_and_import_preserve_selected_source_account_id(tmp_path):
    accounts_db_path = tmp_path / "accounts.db"
    init_db(accounts_db_path)
    with sqlite3.connect(accounts_db_path) as connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id, buffer_account_id, proxy_session, status,
                account_name, buffer_channels, buffer_profile_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ads-2",
                "buffer-2",
                "session-2",
                "active",
                "Brand Two",
                json.dumps([{"id": "tt-2", "displayName": "Brand Two TikTok", "service": "tiktok", "descriptor": "@BrandTwo"}]),
                json.dumps(["tt-2"]),
            ),
        )
    candidate = existing_account_candidates(accounts_db_path)[0]
    store = StatsStore(tmp_path / "stats.db")

    result = import_tracked_accounts(
        store,
        [candidate["username"]],
        source="existing_account",
        source_ids=[candidate["source_account_id"]],
    )

    assert result.results[0].status == "added"
    row = store.connection.execute(
        "SELECT username, username_key, source, source_account_id, sec_uid FROM tracked_accounts"
    ).fetchone()
    assert tuple(row) == ("BrandTwo", "brandtwo", "existing_account", "1", None)
    store.close()
