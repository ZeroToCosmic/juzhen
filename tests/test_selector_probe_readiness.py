import asyncio

import pytest

from selector_probe.readiness import (
    ReadinessError,
    ReadinessToken,
    semantic_similarity,
    wait_for_semantic_readiness,
)


def sample(**overrides):
    value = {
        "origin": "https://www.tiktok.com",
        "ready_state": "interactive",
        "root_visible": True,
        "blocked_marker": "",
        "skeleton_count": 0,
        "feed_visible": True,
        "fingerprints": ["button|comments|data-e2e=comment-icon"],
    }
    value.update(overrides)
    return value


class SamplePage:
    def __init__(self, samples):
        self.samples = list(samples)
        self.sample_count = 0
        self.time = 0.0

    async def evaluate(self, _script):
        index = min(self.sample_count, len(self.samples) - 1)
        self.sample_count += 1
        return self.samples[index]

    async def sleep(self, seconds):
        self.time += seconds

    def monotonic(self):
        return self.time


def test_similarity_uses_jaccard_ratio():
    assert semantic_similarity(
        frozenset({"a", "b"}),
        frozenset({"b", "c"}),
    ) == pytest.approx(1 / 3)


def test_slow_page_waits_for_skeleton_clear_and_semantic_stability():
    async def scenario():
        page = SamplePage(
            [
                sample(ready_state="loading"),
                sample(skeleton_count=1),
                sample(),
                sample(),
                sample(),
            ]
        )
        token, evidence = await wait_for_semantic_readiness(
            page,
            expected_origin="https://www.tiktok.com",
            sleep_fn=page.sleep,
            monotonic_fn=page.monotonic,
        )
        assert isinstance(token, ReadinessToken)
        assert evidence["ready"] is True
        assert evidence["semantic_count"] == 1
        assert page.sample_count == 5

    asyncio.run(scenario())


def test_blocked_page_fails_immediately():
    async def scenario():
        page = SamplePage([sample(blocked_marker="login")])
        with pytest.raises(ReadinessError) as caught:
            await wait_for_semantic_readiness(
                page,
                expected_origin="https://www.tiktok.com",
                sleep_fn=page.sleep,
                monotonic_fn=page.monotonic,
            )
        assert caught.value.code == "probe_page_blocked"
        assert page.sample_count == 1

    asyncio.run(scenario())


def test_unstable_semantics_timeout_without_token():
    async def scenario():
        page = SamplePage(
            [
                sample(fingerprints=["button|a"]),
                sample(fingerprints=["button|b"]),
                sample(fingerprints=["button|a"]),
                sample(fingerprints=["button|b"]),
            ]
        )
        with pytest.raises(ReadinessError) as caught:
            await wait_for_semantic_readiness(
                page,
                expected_origin="https://www.tiktok.com",
                timeout_seconds=3,
                sleep_fn=page.sleep,
                monotonic_fn=page.monotonic,
            )
        assert caught.value.code == "page_readiness_timeout"

    asyncio.run(scenario())


def test_origin_mismatch_fails_closed():
    async def scenario():
        page = SamplePage([sample(origin="https://example.test")])
        with pytest.raises(ReadinessError) as caught:
            await wait_for_semantic_readiness(
                page,
                expected_origin="https://www.tiktok.com",
                sleep_fn=page.sleep,
                monotonic_fn=page.monotonic,
            )
        assert caught.value.code == "probe_origin_mismatch"

    asyncio.run(scenario())
