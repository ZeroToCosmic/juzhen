import copy
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from browser_element_schema import normalize_element_definitions
from selector_probe.managed_runtime import ManagedElementRuntime, ManagedProbeRuntime
from selector_probe.probe import run_managed_probe


def locator(value: str, locator_type: str = "css") -> dict[str, str]:
    return {"type": locator_type, "value": value}


def definition(
    *values: str,
    page_key: str = "feed",
    steps: list[dict[str, object]] | None = None,
    fingerprint: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "page_key": page_key,
        "target_origin": "https://www.tiktok.com",
        "url_pattern": "https://www.tiktok.com/",
        "operation_steps": steps or [],
        "fingerprint": fingerprint or {},
        "locators": [locator(value) for value in values],
    }


class FakeStore:
    def __init__(self, rows, definitions):
        self.rows = list(rows)
        self.definitions = copy.deepcopy(definitions)

    def list_managed_element_rows(self, **kwargs):
        start = (kwargs["page"] - 1) * kwargs["page_size"]
        end = start + kwargs["page_size"]
        return tuple(self.rows[start:end]), len(self.rows), 9

    def manual_element_definition(self, element_id):
        return copy.deepcopy(self.definitions.get(element_id))


class FakeNode:
    def __init__(self, descendants=None):
        self.scrolled = 0
        self.clicked = 0
        self.descendants = dict(descendants or {})

    async def scroll_into_view_if_needed(self):
        self.scrolled += 1

    async def click(self):
        self.clicked += 1

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def evaluate(self, _script):
        return True

    def locator(self, selector):
        return FakeCollection(self.descendants.get(selector, ()))


class FakeCollection:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    async def all(self):
        return list(self.nodes)

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class FakeFrame:
    def __init__(self, css_matches=None, children=None):
        self.css_matches = dict(css_matches or {})
        self.child_frames = list(children or [])

    async def query_selector_all(self, selector):
        return list(self.css_matches.get(selector, ()))

    def locator(self, selector):
        return FakeCollection(self.css_matches.get(selector, ()))


class FakePage:
    def __init__(self, css_matches=None, *, main_frame=None):
        self.css_matches = dict(css_matches or {})
        self.main_frame = main_frame or self
        self.waits = []

    async def query_selector_all(self, selector):
        return list(self.css_matches.get(selector, ()))

    async def wait_until_ready(self, *, next_locator, timeout_ms):
        self.waits.append((next_locator, timeout_ms))


def runtime_with_bundle(*, primary: str, fallback: str) -> ManagedElementRuntime:
    rows = [{
        "id": "element-1",
        "display_name": "Comment",
        "status": "healthy",
        "revision": 3,
    }]
    store = FakeStore(rows, {"element-1": definition(primary, fallback)})
    return ManagedElementRuntime(
        store,
        validator_fn=lambda _page, _definition: {},
        version_id_factory=lambda _moment: "selector-version-id",
    )


def passed(index=0):
    return {
        "status": "passed",
        "failure_code": "",
        "selected_locator_index": index,
    }


def test_load_candidate_filters_status_and_missing_definition():
    rows = [
        {"id": "a", "display_name": "A", "status": "healthy", "revision": 1},
        {"id": "b", "display_name": "B", "status": "degraded", "revision": 2},
        {"id": "c", "display_name": "C", "status": "validating", "revision": 3},
        {"id": "d", "display_name": "D", "status": "draft", "revision": 4},
        {"id": "e", "display_name": "E", "status": "healthy", "revision": 5},
    ]
    store = FakeStore(rows, {
        "a": definition("#a"),
        "b": definition("#b"),
        "c": definition("#c"),
        "d": definition("#d"),
    })

    candidate = ManagedElementRuntime(store).load_candidate()

    assert set(candidate["elements"]) == {"a", "b", "c", "d"}
    assert candidate["elements"]["b"] == {
        "definition": definition("#b"),
        "status": "degraded",
        "revision": 2,
        "display_name": "B",
    }


def test_validate_candidate_replays_scroll_click_and_validates_each_element():
    node = FakeNode()
    step = {"sequence": 1, "locator": locator("#open")}
    rows = [{"id": "a", "display_name": "A", "status": "healthy", "revision": 1}]
    store = FakeStore(rows, {"a": definition("#target", steps=[step])})
    page = FakePage({"#open": [node]})
    seen = []

    async def validate(selected_page, selected_definition):
        seen.append((selected_page, selected_definition["locators"]))
        return passed()

    runtime = ManagedElementRuntime(store, validator_fn=validate)
    result = asyncio.run(
        runtime.validate_candidate(runtime.load_candidate(), page=page)
    )

    assert result["status"] == "passed"
    assert result["elements"]["a"]["selected_locator_index"] == 0
    assert node.scrolled == node.clicked == 1
    assert page.waits == [(None, 90_000)]
    assert seen[0][0].page is page
    assert seen[0][1] == [locator("#target")]


def test_missing_step_fails_only_its_operation_chain():
    available = FakeNode()
    missing_step = {"sequence": 1, "locator": locator("#missing")}
    other_step = {"sequence": 1, "locator": locator("#available")}
    rows = [
        {"id": "bad-1", "display_name": "B1", "status": "healthy", "revision": 1},
        {"id": "bad-2", "display_name": "B2", "status": "degraded", "revision": 1},
        {"id": "good", "display_name": "G", "status": "validating", "revision": 1},
    ]
    definitions = {
        "bad-1": definition("#bad-1", steps=[missing_step]),
        "bad-2": definition("#bad-2", steps=[missing_step]),
        "good": definition("#good", page_key="other", steps=[other_step]),
    }
    store = FakeStore(rows, definitions)
    pages = {
        "feed": FakePage(),
        "other": FakePage({"#available": [available]}),
    }

    async def validate(_page, _definition):
        return passed()

    runtime = ManagedElementRuntime(
        store,
        page_provider=lambda selected: pages[selected["page_key"]],
        validator_fn=validate,
        page_ready_timeout_seconds=0.01,
    )
    result = asyncio.run(runtime.validate_candidate(runtime.load_candidate()))

    assert result["status"] == "failed"
    assert result["elements"]["bad-1"]["failure_code"] == "recorded_step_unavailable"
    assert result["elements"]["bad-2"]["failure_code"] == "recorded_step_unavailable"
    assert result["elements"]["good"]["status"] == "passed"
    assert available.clicked == 1


def test_promote_saved_fallbacks_only_uses_valid_passing_index():
    runtime = runtime_with_bundle(primary="#old", fallback='[data-e2e="new"]')
    candidate = runtime.load_candidate()

    promoted = runtime.promote_saved_fallbacks(candidate, {
        "elements": {"element-1": passed(1)}
    })
    assert promoted["elements"]["element-1"]["definition"]["locators"][0]["value"] == '[data-e2e="new"]'
    assert candidate["elements"]["element-1"]["definition"]["locators"][0]["value"] == "#old"

    for result in (
        {"status": "failed", "selected_locator_index": 1},
        {"status": "passed", "selected_locator_index": 8},
        {"status": "passed", "selected_locator_index": True},
    ):
        unchanged = runtime.promote_saved_fallbacks(candidate, {
            "elements": {"element-1": result}
        })
        assert unchanged == candidate


def test_prepare_publication_is_registry_compatible_and_canonically_hashed():
    runtime = runtime_with_bundle(primary="#old", fallback='[data-e2e="new"]')
    validation = {
        "elements": {"element-1": passed(1)}
    }
    promoted = runtime.promote_saved_fallbacks(runtime.load_candidate(), validation)
    bundle = runtime.prepare_publication(promoted, validation)

    assert bundle["version"] == "selector-version-id"
    assert bundle["elements"]["element-1"]["scope"] == "page"
    assert [item["value"] for item in bundle["elements"]["element-1"]["locators"]] == [
        '[data-e2e="new"]', "#old"
    ]
    assert all(item["enabled"] is True for item in bundle["elements"]["element-1"]["locators"])
    assert all(item["type"] in {"css", "xpath"} for item in bundle["elements"]["element-1"]["locators"])
    assert "operation_steps" not in json.dumps(bundle)
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            bundle["elements"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert bundle["bundle_hash"] == expected
    assert normalize_element_definitions(bundle["elements"]) == bundle["elements"]


def test_prepare_publication_rejects_unpassed_or_non_css_xpath_data():
    runtime = runtime_with_bundle(primary="#old", fallback="#new")
    candidate = runtime.load_candidate()
    with pytest.raises(ValueError, match="must pass"):
        runtime.prepare_publication(candidate, {
            "elements": {"element-1": {"status": "failed"}}
        })

    candidate["elements"]["element-1"]["definition"]["locators"][0] = {
        "type": "role", "value": "button"
    }
    with pytest.raises(ValueError, match="candidate locators"):
        runtime.prepare_publication(candidate, {
            "elements": {"element-1": passed(0)}
        })


def test_candidate_rejects_non_exact_or_unsafe_saved_locators():
    runtime = runtime_with_bundle(primary="#old", fallback="#new")
    for invalid in (
        {"type": "css", "value": " #old"},
        {"type": "css", "value": ".generated-class"},
        {"type": "css", "value": "#old", "enabled": True},
        {"type": "role", "value": "button"},
    ):
        candidate = runtime.load_candidate()
        candidate["elements"]["element-1"]["definition"]["locators"][0] = invalid
        with pytest.raises(ValueError, match="candidate locators"):
            runtime.promote_saved_fallbacks(candidate, {"elements": {}})


def test_malformed_or_non_contiguous_steps_fail_as_recorded_step_unavailable():
    rows = [{"id": "a", "display_name": "A", "status": "healthy", "revision": 1}]
    malformed = definition("#target", steps=[{
        "sequence": 2,
        "locator": locator("#open"),
        "frame_key": "main",
        "shadow": False,
        "shadow_key": "document",
    }])
    runtime = ManagedElementRuntime(FakeStore(rows, {"a": malformed}))

    result = asyncio.run(runtime.validate_candidate(runtime.load_candidate(), page=FakePage()))

    assert result["elements"]["a"]["failure_code"] == "recorded_step_unavailable"


def test_target_validation_is_scoped_to_iframe_and_open_shadow_root():
    iframe_target = FakeNode()
    shadow_target = FakeNode()
    top_duplicates = [FakeNode(), FakeNode()]
    child = FakeFrame({"#target": [iframe_target]})
    host = FakeNode({"#target": [shadow_target]})
    main = FakeFrame(
        {
            "#target": top_duplicates,
            '[data-testid="host"]': [host],
        },
        children=[child],
    )
    page = FakePage(main_frame=main)
    rows = [
        {"id": "iframe", "display_name": "I", "status": "healthy", "revision": 1},
        {"id": "shadow", "display_name": "S", "status": "healthy", "revision": 1},
    ]
    definitions = {
        "iframe": definition(
            "#target",
            fingerprint={
                "frame_key": "main/frame:1",
                "shadow": False,
                "shadow_key": "document",
            },
        ),
        "shadow": definition(
            "#target",
            fingerprint={
                "frame_key": "main",
                "shadow": True,
                "shadow_key": 'document/[data-testid="host"]',
            },
        ),
    }
    runtime = ManagedElementRuntime(FakeStore(rows, definitions))

    result = asyncio.run(runtime.validate_candidate(runtime.load_candidate(), page=page))

    assert result["elements"]["iframe"]["status"] == "passed"
    assert result["elements"]["shadow"]["status"] == "passed"


def test_step_replay_preserves_iframe_and_shadow_context():
    iframe_action = FakeNode()
    shadow_action = FakeNode()
    top_actions = [FakeNode(), FakeNode()]
    child = FakeFrame({"#open": [iframe_action]})
    host = FakeNode({"#confirm": [shadow_action]})
    main = FakeFrame(
        {
            "#open": top_actions,
            "#confirm": top_actions,
            '[data-testid="host"]': [host],
        },
        children=[child],
    )
    steps = [
        {
            "sequence": 1,
            "locator": locator("#open"),
            "frame_key": "main/frame:1",
            "shadow": False,
            "shadow_key": "document",
        },
        {
            "sequence": 2,
            "locator": locator("#confirm"),
            "frame_key": "main",
            "shadow": True,
            "shadow_key": 'document/[data-testid="host"]',
        },
    ]
    rows = [{"id": "a", "display_name": "A", "status": "healthy", "revision": 1}]
    runtime = ManagedElementRuntime(
        FakeStore(rows, {"a": definition("#target", steps=steps)}),
        validator_fn=lambda _page, _definition: passed(),
    )

    result = asyncio.run(
        runtime.validate_candidate(runtime.load_candidate(), page=FakePage(main_frame=main))
    )

    assert result["elements"]["a"]["status"] == "passed"
    assert iframe_action.clicked == shadow_action.clicked == 1
    assert all(node.clicked == 0 for node in top_actions)


def test_six_saved_locators_publish_five_after_promoting_the_sixth():
    values = [f"#saved{index}" for index in range(1, 7)]
    rows = [{"id": "element-1", "display_name": "E", "status": "healthy", "revision": 1}]
    runtime = ManagedElementRuntime(
        FakeStore(rows, {"element-1": definition(*values)}),
        version_id_factory=lambda _moment: "selector-version-id",
    )

    validation = {
        "elements": {"element-1": passed(5)}
    }
    promoted = runtime.promote_saved_fallbacks(runtime.load_candidate(), validation)
    bundle = runtime.prepare_publication(promoted, validation)

    published = bundle["elements"]["element-1"]["locators"]
    assert len(published) == 5
    assert [item["value"] for item in published] == [
        "#saved6", "#saved1", "#saved2", "#saved3", "#saved4"
    ]


class FakeMatrixPage:
    def __init__(self, name, *, readiness_samples=None):
        self.name = name
        self.goto_calls = []
        self.reload_calls = 0
        self.closed = False
        self.readiness_samples = list(readiness_samples or [{
            "origin": "https://www.tiktok.com",
            "body_visible": True,
            "interactive_count": 3,
        }])
        self.readiness_sample_count = 0

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    async def reload(self, **_kwargs):
        self.reload_calls += 1

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def evaluate(self, _script):
        index = min(
            self.readiness_sample_count,
            len(self.readiness_samples) - 1,
        )
        self.readiness_sample_count += 1
        return copy.deepcopy(self.readiness_samples[index])

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeSession:
    def __init__(self, _client, *, allowed_profile_ids, **_kwargs):
        self.profiles = [
            SimpleNamespace(
                profile_id=value,
                profile_mask=f"***000{index + 1}",
                started_by_probe=True,
            )
            for index, value in enumerate(allowed_profile_ids)
        ]
        self.pages = [FakeMatrixPage(f"p{index + 1}") for index in range(len(self.profiles))]
        self.closed_pages = False
        self.stopped_profiles = False

    def open_profiles(self, _profile_ids):
        return list(self.profiles)

    async def open_probe_page(self, _playwright, profile):
        index = self.profiles.index(profile)
        return SimpleNamespace(profile=profile, page=self.pages[index])

    async def close_owned_pages(self, handles):
        self.closed_pages = True
        for handle in handles:
            await handle.page.close()
        return [{"ok": True} for _handle in handles]

    def stop_owned_profiles(self, handles):
        self.stopped_profiles = True
        return [{"ok": True} for _handle in handles]


class FakePublishStore(FakeStore):
    def __init__(self, rows, definitions):
        super().__init__(rows, definitions)
        self.saved = None
        self.versions = {}

    def store_validated_version(self, **payload):
        self.saved = copy.deepcopy(payload)
        self.versions["version-1"] = {"status": "validated"}
        return "version-1"

    def get_version(self, version):
        return copy.deepcopy(self.versions.get(version))


class FakeRegistry:
    def __init__(self):
        self.active = {"version": "base-version"}

    def get_active(self):
        return copy.deepcopy(self.active)


def managed_probe_fixture(*, validator_fn, progress_sink=None):
    rows = [
        {"id": "a", "display_name": "A", "status": "healthy", "revision": 1},
        {"id": "b", "display_name": "B", "status": "degraded", "revision": 2},
    ]
    store = FakePublishStore(rows, {"a": definition("#a"), "b": definition("#b")})
    session_box = {}
    playwright = FakePlaywright()

    def session_factory(*args, **kwargs):
        session = FakeSession(*args, **kwargs)
        session_box["value"] = session
        return session

    runtime = ManagedProbeRuntime(
        config=SimpleNamespace(
            test_profile_ids=("profile-one", "profile-two"),
            target_url="https://www.tiktok.com/",
            site="tiktok",
            environment="production",
        ),
        settings={},
        store=store,
        registry=FakeRegistry(),
        adspower_client=object(),
        validator_fn=validator_fn,
        session_manager_factory=session_factory,
        playwright_starter=lambda: playwright,
        wait_for_cdp=lambda _url: True,
        progress_sink=progress_sink,
        page_ready_timeout_seconds=0.1,
        page_stability_interval_seconds=0.01,
    )
    return runtime, store, session_box, playwright


def test_managed_probe_opens_two_profiles_two_rounds_and_cleans_everything():
    calls = []

    async def validate(page, selected_definition):
        calls.append((page.page.name, selected_definition["locators"][0]["value"]))
        return passed(0)

    progress = []
    runtime, _store, session_box, playwright = managed_probe_fixture(
        validator_fn=validate,
        progress_sink=progress.append,
    )
    with runtime:
        result = runtime.validate_matrix(runtime.load_candidate())

    session = session_box["value"]
    assert result["status"] == "passed"
    assert result["consistent"] is True
    assert result["profiles_passed"] == 2
    assert result["rounds_passed"] == 2
    assert len(result["profile_results"]) == 4
    assert len(result["validations"]) == 4
    assert all(
        set(row) == {
            "profile_mask",
            "round_number",
            "reset_evidence_hash",
            "snapshot_hash",
            "page_generation",
            "aliases",
        }
        and "elements" not in row
        and set(row["aliases"]) == {"a", "b"}
        and all(value["status"] == "ok" for value in row["aliases"].values())
        for row in result["validations"]
    )
    assert len(calls) == 8
    assert all(len(page.goto_calls) == 1 for page in session.pages)
    assert all(page.reload_calls == 1 for page in session.pages)
    assert session.closed_pages is True
    assert session.stopped_profiles is True
    assert playwright.stopped is True
    assert {event["name"] for event in progress} >= {
        "prepare_environment", "open_and_replay", "validate_elements"
    }


def test_matrix_retries_only_failed_element_and_requires_consistent_index():
    counts = {"#a": 0, "#b": 0}

    async def validate(_page, selected_definition):
        value = selected_definition["locators"][0]["value"]
        counts[value] += 1
        if value == "#b" and counts[value] < 3:
            return {"status": "failed", "failure_code": "selector_zero_match"}
        return passed(0)

    runtime, _store, _session, _playwright = managed_probe_fixture(
        validator_fn=validate
    )
    with runtime:
        result = runtime.validate_matrix(runtime.load_candidate())

    assert result["status"] == "passed"
    assert counts == {"#a": 4, "#b": 6}

    calls = 0

    async def inconsistent(_page, _definition):
        nonlocal calls
        calls += 1
        return passed(1 if calls == 4 else 0)

    one_rows = [{"id": "a", "display_name": "A", "status": "healthy", "revision": 1}]
    runtime, _store, _session, _playwright = managed_probe_fixture(
        validator_fn=inconsistent
    )
    runtime.store.rows = one_rows
    runtime.store.definitions = {"a": definition("#a", "#fallback")}
    with runtime:
        inconsistent_result = runtime.validate_matrix(runtime.load_candidate())
    assert inconsistent_result["status"] == "failed"
    assert inconsistent_result["elements"]["a"]["failure_code"] == "selector_inconsistent"


def test_matrix_stops_terminal_failed_element_after_three_total_calls():
    counts = {"#a": 0, "#b": 0}

    async def validate(_page, selected_definition):
        value = selected_definition["locators"][0]["value"]
        counts[value] += 1
        if value == "#b":
            return {"status": "failed", "failure_code": "selector_zero_match"}
        return passed(0)

    runtime, _store, _session, _playwright = managed_probe_fixture(
        validator_fn=validate
    )
    with runtime:
        result = runtime.validate_matrix(runtime.load_candidate())

    assert result["status"] == "failed"
    assert result["consistent"] is False
    assert counts == {"#a": 4, "#b": 3}
    assert result["elements"]["b"]["attempt_count"] == 3
    assert len(result["elements"]["b"]["profile_results"]) == 4


def test_store_and_publish_builds_legacy_evidence_and_requires_atomic_publish():
    async def validate(_page, _definition):
        return passed(0)

    runtime, store, _session, _playwright = managed_probe_fixture(
        validator_fn=validate
    )

    def reconciler(selected_store, _registry):
        selected_store.versions["version-1"] = {"status": "published"}
        return {"version": "version-1", "acknowledged": 1}

    runtime.reconciler = reconciler
    with runtime:
        candidate = runtime.load_candidate()
        validation = runtime.validate_matrix(candidate)
        bundle = runtime.prepare_publication(candidate, validation)
        published = runtime.store_and_publish(bundle, validation)

    assert published == {
        "version": "version-1", "published": True, "reconciled": True
    }
    assert store.saved["base_version_id"] == "base-version"
    assert store.saved["model_id"] == store.saved["prompt_version"] == ""
    assert set(store.saved["bundle"]) == {"bundle_hash", "elements"}
    assert store.saved["evidence"]["profiles_passed"] == 2
    assert len(store.saved["evidence"]["validations"]) == 4
    assert all(
        set(row["aliases"]) == {"a", "b"}
        for row in store.saved["evidence"]["validations"]
    )


def test_store_and_publish_rejects_unacknowledged_atomic_publication():
    async def validate(_page, _definition):
        return passed(0)

    runtime, _store, _session, _playwright = managed_probe_fixture(
        validator_fn=validate
    )
    runtime.reconciler = lambda _store, _registry: {
        "version": "version-1", "acknowledged": 0
    }
    with runtime:
        candidate = runtime.load_candidate()
        validation = runtime.validate_matrix(candidate)
        bundle = runtime.prepare_publication(candidate, validation)
        with pytest.raises(RuntimeError, match="selector_publish_failed"):
            runtime.store_and_publish(bundle, validation)


def test_real_managed_runtime_composes_with_run_managed_probe_without_double_promote():
    async def validate(_page, selected_definition):
        return passed(1 if len(selected_definition["locators"]) == 2 else 0)

    runtime, store, _session, _playwright = managed_probe_fixture(
        validator_fn=validate
    )
    store.definitions["b"] = definition("#old-b", "#validated-b")

    def reconciler(selected_store, _registry):
        selected_store.versions["version-1"] = {"status": "published"}
        return {"version": "version-1", "acknowledged": 1}

    runtime.reconciler = reconciler
    with runtime:
        result = run_managed_probe(runtime)

    assert result["status"] == "published"
    assert store.saved["bundle"]["elements"]["b"]["locators"][0]["value"] == "#validated-b"
    selected_id = store.saved["bundle"]["elements"]["b"]["locators"][0]["id"]
    assert all(
        row["aliases"]["b"]["candidate_id"] == selected_id
        for row in store.saved["evidence"]["validations"]
    )


def test_navigation_failure_cancels_siblings_and_still_cleans_owned_resources():
    class FailingPage(FakeMatrixPage):
        async def goto(self, _url, **_kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("navigation failed")

    class BlockingPage(FakeMatrixPage):
        def __init__(self, name):
            super().__init__(name)
            self.cancelled = False

        async def goto(self, _url, **_kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    session_box = {}
    playwright = FakePlaywright()
    progress = []

    def session_factory(*args, **kwargs):
        session = FakeSession(*args, **kwargs)
        session.pages = [FailingPage("failed"), BlockingPage("blocked")]
        session_box["value"] = session
        return session

    runtime = ManagedProbeRuntime(
        config=SimpleNamespace(
            test_profile_ids=("profile-one", "profile-two"),
            target_url="https://www.tiktok.com/",
            site="tiktok",
            environment="production",
        ),
        settings={},
        store=FakeStore([], {}),
        registry=FakeRegistry(),
        adspower_client=object(),
        session_manager_factory=session_factory,
        playwright_starter=lambda: playwright,
        wait_for_cdp=lambda _url: True,
        progress_sink=progress.append,
        page_ready_timeout_seconds=0.1,
        page_stability_interval_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="navigation failed"):
        with runtime:
            pass

    session = session_box["value"]
    assert session.pages[1].cancelled is True
    assert session.closed_pages is True
    assert session.stopped_profiles is True
    assert playwright.stopped is True
    assert any(
        item.get("name") == "open_and_replay"
        and item.get("status") == "failed"
        for item in progress
    )


def test_page_readiness_waits_for_two_stable_positive_interactive_samples():
    page = FakeMatrixPage("slow", readiness_samples=[
        {
            "origin": "https://www.tiktok.com",
            "body_visible": True,
            "interactive_count": 0,
        },
        {
            "origin": "https://www.tiktok.com",
            "body_visible": True,
            "interactive_count": 4,
        },
        {
            "origin": "https://www.tiktok.com",
            "body_visible": True,
            "interactive_count": 7,
        },
        {
            "origin": "https://www.tiktok.com",
            "body_visible": True,
            "interactive_count": 7,
        },
    ])
    runtime = ManagedProbeRuntime(
        config=SimpleNamespace(
            test_profile_ids=("profile-one", "profile-two"),
            target_url="https://www.tiktok.com/",
        ),
        settings={},
        store=FakeStore([], {}),
        registry=FakeRegistry(),
        adspower_client=object(),
        page_ready_timeout_seconds=0.2,
        page_stability_interval_seconds=0.005,
    )

    asyncio.run(runtime._page_ready(page, reload_page=False))

    assert page.readiness_sample_count == 4


def test_page_readiness_times_out_while_interactive_content_is_absent():
    page = FakeMatrixPage("skeleton", readiness_samples=[{
        "origin": "https://www.tiktok.com",
        "body_visible": True,
        "interactive_count": 0,
    }])
    runtime = ManagedProbeRuntime(
        config=SimpleNamespace(
            test_profile_ids=("profile-one", "profile-two"),
            target_url="https://www.tiktok.com/",
        ),
        settings={},
        store=FakeStore([], {}),
        registry=FakeRegistry(),
        adspower_client=object(),
        page_ready_timeout_seconds=0.03,
        page_stability_interval_seconds=0.005,
    )

    with pytest.raises(RuntimeError, match="page_readiness_timeout"):
        asyncio.run(runtime._page_ready(page, reload_page=False))

    assert page.closed is False


def test_element_attempt_polls_delayed_render_without_refresh_or_early_close():
    states = [
        {"status": "failed", "failure_code": "selector_zero_match"},
        {"status": "failed", "failure_code": "selector_hidden"},
        passed(0),
    ]
    calls = 0

    async def delayed(_page, _definition):
        nonlocal calls
        calls += 1
        return copy.deepcopy(states.pop(0) if states else passed(0))

    runtime, store, session_box, _playwright = managed_probe_fixture(
        validator_fn=delayed
    )
    store.rows = [
        {"id": "a", "display_name": "A", "status": "healthy", "revision": 1}
    ]
    store.definitions = {"a": definition("#a")}
    runtime.element_poll_interval_seconds = 0.001
    with runtime:
        result = runtime.validate_matrix(runtime.load_candidate())
        session = session_box["value"]
        assert all(page.closed is False for page in session.pages)
        assert session.pages[0].reload_calls == 1

    assert result["status"] == "passed"
    assert result["elements"]["a"]["attempt_count"] == 1
    assert calls == 6


def test_element_readiness_timeout_refreshes_only_before_next_global_retry():
    calls_before_refresh = 0

    async def waits_for_refresh(page, _definition):
        nonlocal calls_before_refresh
        selected_page = page.page
        if selected_page.name == "p1" and selected_page.reload_calls == 0:
            calls_before_refresh += 1
            return {"status": "failed", "failure_code": "selector_zero_match"}
        return passed(0)

    runtime, store, session_box, _playwright = managed_probe_fixture(
        validator_fn=waits_for_refresh
    )
    store.rows = [
        {"id": "a", "display_name": "A", "status": "healthy", "revision": 1}
    ]
    store.definitions = {"a": definition("#a")}
    runtime.page_ready_timeout_seconds = 0.02
    runtime.element_poll_interval_seconds = 0.002
    with runtime:
        result = runtime.validate_matrix(runtime.load_candidate())
        pages = session_box["value"].pages
        assert pages[0].reload_calls == 2
        assert pages[1].reload_calls == 1

    assert calls_before_refresh >= 2
    assert result["status"] == "passed"
    assert result["elements"]["a"]["attempt_count"] == 2
