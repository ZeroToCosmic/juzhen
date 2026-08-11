"""Real AdsPower smoke acceptance for the agent kernel (M4 manual step).

Verifies the real execution chain against a live AdsPower instance:

  mode=link      profile start -> CDP connect -> navigate -> screenshot -> close -> stop
  mode=strategy  additionally runs a minimal scroll strategy through the
                 agent ExecutionV2Executor with a human-provided readiness
                 selector (e.g. a stable TikTok page element).

Requires: AdsPower running locally, a profile with TikTok logged in, and
the Playwright browser binaries installed.

Usage:
  python scripts/smoke_adspower.py --mode link --profile-id <id>
  python scripts/smoke_adspower.py --mode strategy --profile-id <id> ^
      --ready-selector "div[data-e2e='feed-content']"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from adspower import AdsPowerController  # noqa: E402
from execution_v2.adspower_adapter import RateLimitedAdsPowerAdapter  # noqa: E402
from execution_v2.actions import execute_action  # noqa: E402
from execution_v2.executor import StrategyExecutor  # noqa: E402
from execution_v2.locator import StrictLocatorResolver  # noqa: E402
from execution_v2.session import PlaywrightSessionFactory  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

DEFAULT_TARGET = "https://www.tiktok.com/"


def build_adspower() -> RateLimitedAdsPowerAdapter:
    settings = {}
    try:
        from gateway.settings_store import load_settings

        settings = load_settings()
    except BaseException:
        pass
    adspower_cfg = settings.get("adspower", {})
    base_url = adspower_cfg.get("base_url") or "http://local.adspower.net:50325"
    api_key = adspower_cfg.get("api_key") or ""
    controller = AdsPowerController(base_url=base_url, api_key=api_key)
    return RateLimitedAdsPowerAdapter(controller)


async def run_link(adspower, profile_id: str, target_url: str, evidence_dir: Path) -> int:
    print(f"[smoke] link mode profile={profile_id} target={target_url}")
    async with async_playwright() as playwright:
        session_factory = PlaywrightSessionFactory(playwright)
        ws_url = await adspower.start(profile_id)
        print(f"[smoke] AdsPower started, ws={ws_url[:40]}...")
        binding = await session_factory.connect(profile_id, ws_url)
        print(f"[smoke] CDP connected, page url={binding.page.url}")
        await binding.page.goto(target_url, timeout=30_000)
        print(f"[smoke] navigated to {binding.page.url}")
        evidence_dir.mkdir(parents=True, exist_ok=True)
        shot = evidence_dir / f"link-{profile_id}.png"
        await binding.page.screenshot(path=str(shot))
        print(f"[smoke] screenshot saved: {shot}")
        await binding.browser.close()
        await adspower.stop(profile_id)
        print("[smoke] profile closed and stopped")
    return 0


async def run_strategy(adspower, profile_id: str, target_url: str, ready_selector: str, evidence_dir: Path) -> int:
    print(f"[smoke] strategy mode profile={profile_id} selector={ready_selector}")
    async with async_playwright() as playwright:
        session_factory = PlaywrightSessionFactory(playwright)
        resolver = StrictLocatorResolver()

        def strategy_executor_factory():
            return StrategyExecutor(
                resolver,
                action_executor=execute_action,
            )

        from agent.execution_v2_executor import ExecutionV2Executor

        adapter = ExecutionV2Executor(
            adspower,
            session_factory,
            strategy_executor=strategy_executor_factory(),
        )
        snapshot = {
            "strategy": {
                "target_url": target_url,
                "ready_element_id": "ready",
                "readiness_timeout_seconds": 20,
                "actions": [
                    {
                        "id": "scroll-1",
                        "type": "scroll_down",
                        "params": {"distance": 400},
                    }
                ],
            },
            "elements": {
                "ready": {
                    "definition": {
                        "locators": [{"id": "smoke-ready", "type": "css", "selector": ready_selector}],
                    }
                }
            },
            "wheel_calibration": {"scale": 1.0},
        }
        subtask = {
            "subtask_id": "smoke-strategy-1",
            "profile_id": profile_id,
            "config_snapshot": snapshot,
        }
        outcome = await asyncio.to_thread(adapter.execute, subtask)
        print(f"[smoke] outcome status={outcome.status} error_code={outcome.error_code}")
        if outcome.status != "SUCCESS":
            print(f"[smoke] FAILED: {outcome.result_data}")
            return 1
        print(f"[smoke] SUCCESS: {outcome.result_data}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent kernel AdsPower smoke acceptance")
    parser.add_argument("--mode", choices=["link", "strategy"], required=True)
    parser.add_argument("--profile-id", required=True, help="AdsPower profile id")
    parser.add_argument("--target-url", default=DEFAULT_TARGET)
    parser.add_argument("--ready-selector", default="", help="CSS selector for readiness (strategy mode)")
    parser.add_argument("--evidence-dir", default=str(PROJECT_ROOT / "data" / "agent-smoke"))
    args = parser.parse_args()

    if args.mode == "strategy" and not args.ready_selector:
        parser.error("--ready-selector is required for strategy mode")

    adspower = build_adspower()
    evidence_dir = Path(args.evidence_dir)
    if args.mode == "link":
        return asyncio.run(run_link(adspower, args.profile_id, args.target_url, evidence_dir))
    return asyncio.run(
        run_strategy(adspower, args.profile_id, args.target_url, args.ready_selector, evidence_dir)
    )


if __name__ == "__main__":
    sys.exit(main())
