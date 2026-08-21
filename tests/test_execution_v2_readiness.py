import asyncio

import pytest

from execution_v2.locator import LocatorResolutionError, ResolvedElement
from execution_v2.readiness import PageReadinessError, ReadinessTimeout, wait_until_ready


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


class FakePage:
    def __init__(self, url="https://www.tiktok.com/@account/video/1"):
        self.url = url


class FakeResolver:
    def __init__(self, samples):
        self.samples = iter(samples)
        self.calls = 0

    async def resolve(self, page, definition, *, require_editable=False):
        self.calls += 1
        value = next(self.samples)
        if isinstance(value, Exception):
            raise value
        return ResolvedElement(handle=object(), locator_type="css", box=value, diagnostics=())


def definition():
    return {"url_pattern": "https://www.tiktok.com/*", "frame_path": [], "locators": []}


def wait(page, resolver, *, timeout=5, clock=None):
    clock = clock or FakeClock()
    return asyncio.run(
        wait_until_ready(
            page,
            definition(),
            resolver,
            timeout_seconds=timeout,
            sleep=clock.sleep,
            clock=clock,
        )
    )


def test_readiness_passes_after_exactly_three_stable_visible_samples_without_real_sleep():
    clock = FakeClock()
    box = {"x": 10, "y": 20, "width": 100, "height": 40}
    resolver = FakeResolver([box, box, box])

    resolved = wait(FakePage(), resolver, clock=clock)

    assert resolved.box == box
    assert resolver.calls == 3
    assert clock.value == 1.0


def test_readiness_resets_counter_when_box_changes_then_requires_three_new_stable_samples():
    clock = FakeClock()
    one = {"x": 10, "y": 20, "width": 100, "height": 40}
    two = {"x": 11, "y": 20, "width": 100, "height": 40}
    resolver = FakeResolver([one, one, two, two, two])

    resolved = wait(FakePage(), resolver, clock=clock)

    assert resolved.box == two
    assert resolver.calls == 5


def test_readiness_resolver_failure_resets_stability_and_timeout_retains_last_diagnostic():
    clock = FakeClock()
    box = {"x": 10, "y": 20, "width": 100, "height": 40}
    failure = LocatorResolutionError("no_valid_locator", ({"code": "locator_not_found"},))
    resolver = FakeResolver([box, failure, box, failure, box])

    with pytest.raises(ReadinessTimeout) as caught:
        wait(FakePage(), resolver, timeout=2, clock=clock)

    assert caught.value.code == "readiness_timeout"
    assert caught.value.diagnostics["last_locator_error"]["code"] == "no_valid_locator"
    assert clock.value == 2.0


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("about:blank", "page_url_blank"),
        ("https://accounts.example.test/login", "page_url_mismatch"),
    ],
)
def test_readiness_rejects_blank_or_unexpected_page_before_polling(url, code):
    clock = FakeClock()
    resolver = FakeResolver([])

    with pytest.raises(PageReadinessError) as caught:
        wait(FakePage(url), resolver, clock=clock)

    assert caught.value.code == code
    assert resolver.calls == 0
