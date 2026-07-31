from gateway.r2_client import list_r2_video_objects


def test_list_r2_video_objects_uses_cloudflare_api_when_endpoint_is_missing():
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "result": [
                    {"key": "video-one.mp4"},
                    {"key": "notes.txt"},
                ]
            }

    def fake_get(url, headers, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        captured["timeout"] = timeout
        return FakeResponse()

    videos = list_r2_video_objects(
        {
            "r2": {
                "account_id": "account-123",
                "account_token": "token-abc",
                "bucket": "videos",
                "public_base_url": "https://cdn.example.com",
                "prefix": "clips/",
            }
        },
        get=fake_get,
    )

    assert captured == {
        "url": "https://api.cloudflare.com/client/v4/accounts/account-123/r2/buckets/videos/objects",
        "headers": {"Authorization": "Bearer token-abc"},
        "params": {"prefix": "clips/", "per_page": 1000},
        "timeout": 30,
    }
    assert videos == [
        {"key": "video-one.mp4", "url": "https://cdn.example.com/video-one.mp4"}
    ]


def test_list_r2_video_objects_prefers_s3_credentials():
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise AssertionError("S3 XML response should not be parsed as JSON")

        text = """
        <ListBucketResult>
          <Contents><Key>clips/one.mp4</Key></Contents>
          <Contents><Key>clips/readme.txt</Key></Contents>
          <Contents><Key>clips/two.mov</Key></Contents>
        </ListBucketResult>
        """

    def fake_get(url, headers, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        captured["timeout"] = timeout
        return FakeResponse()

    videos = list_r2_video_objects(
        {
            "r2": {
                "account_id": "account-123",
                "access_key_id": "access-key",
                "secret_access_key": "secret-key",
                "bucket": "videos",
                "prefix": "clips/",
                "public_base_url": "https://cdn.example.com",
            }
        },
        get=fake_get,
    )

    assert captured["url"] == "https://account-123.r2.cloudflarestorage.com/videos"
    assert captured["params"] == {
        "list-type": "2",
        "prefix": "clips/",
        "max-keys": "1000",
    }
    assert captured["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert captured["headers"]["x-amz-content-sha256"]
    assert captured["timeout"] == 30
    assert videos == [
        {"key": "clips/one.mp4", "url": "https://cdn.example.com/clips/one.mp4"},
        {"key": "clips/two.mov", "url": "https://cdn.example.com/clips/two.mov"},
    ]


def test_list_r2_video_objects_encodes_public_urls_from_s3_keys():
    class FakeResponse:
        def raise_for_status(self):
            return None

        text = """
        <ListBucketResult>
          <Contents><Key>背景/0710 (1).mp4</Key></Contents>
        </ListBucketResult>
        """

    videos = list_r2_video_objects(
        {
            "r2": {
                "account_id": "account-123",
                "access_key_id": "access-key",
                "secret_access_key": "secret-key",
                "bucket": "videos",
                "public_base_url": "https://media.ttvid.org",
            }
        },
        get=lambda url, headers, params, timeout: FakeResponse(),
    )

    assert videos == [
        {
            "key": "背景/0710 (1).mp4",
            "url": "https://media.ttvid.org/%E8%83%8C%E6%99%AF/0710%20(1).mp4",
        }
    ]


def test_list_r2_video_objects_reads_s3_xml_as_utf8_bytes():
    xml = """
    <ListBucketResult>
      <Contents><Key>背景/0710 (1).mp4</Key></Contents>
    </ListBucketResult>
    """

    class FakeResponse:
        def raise_for_status(self):
            return None

        content = xml.encode("utf-8")
        text = content.decode("latin-1")

    videos = list_r2_video_objects(
        {
            "r2": {
                "account_id": "account-123",
                "access_key_id": "access-key",
                "secret_access_key": "secret-key",
                "bucket": "videos",
                "public_base_url": "https://media.ttvid.org",
            }
        },
        get=lambda url, headers, params, timeout: FakeResponse(),
    )

    assert videos == [
        {
            "key": "背景/0710 (1).mp4",
            "url": "https://media.ttvid.org/%E8%83%8C%E6%99%AF/0710%20(1).mp4",
        }
    ]
