from browser_public_identity import mask_profile_id


def test_profile_id_keeps_only_last_four_characters():
    assert mask_profile_id("profile-k1dxxcto") == "***xcto"


def test_profile_id_mask_is_idempotent():
    assert mask_profile_id("***xcto") == "***xcto"


def test_short_profile_id_reveals_no_characters():
    assert mask_profile_id("abc") == "***"
