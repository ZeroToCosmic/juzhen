# Selector Probe Early-Close Regression Design

## Goal

Prevent validation Profiles from opening and immediately closing when no runnable manual elements exist, and ensure TikTok page loading is not reported complete before the page is stable enough for locator validation.

## Scope

- Check saved runnable elements before starting AdsPower Profiles.
- Treat an empty runnable catalog as `awaiting_element_selection`, not infrastructure failure.
- Preserve environment and navigation stage results; candidate loading must not overwrite them.
- Wait up to the configured 90 seconds for generic page stability: expected TikTok origin, visible body, at least one visible interactive element, and the same positive interactive count in two consecutive samples.
- Keep semantic/LLM matching removed.
- Keep two Profiles, two rounds, retry, publication, LKG, alert, and strategy-gate behavior unchanged.

## Flow

1. Worker loads the manual candidate from SQLite without opening AdsPower.
2. Empty candidate ends the run with `awaiting_element_selection`; UI directs the operator to collect or rebind elements.
3. Non-empty candidate opens two allowlisted Profiles and one owned page per Profile.
4. Each page navigates to TikTok and passes generic stability readiness.
5. Existing recorded-step replay and selector validation run unchanged.
6. Runtime closes owned pages and Profiles only after completion or a real timeout/failure.

## Acceptance

- Empty catalog starts zero Profiles.
- Empty catalog does not mark environment or TikTok loading failed.
- A page with only `domcontentloaded` but no stable interactive content remains open and waiting.
- A stable page proceeds to the existing 2 × 2 validation matrix.
- Stage records cannot be downgraded by candidate availability checks.

