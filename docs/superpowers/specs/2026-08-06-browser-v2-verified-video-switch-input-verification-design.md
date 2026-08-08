# Browser V2 Verified Video Switch and Input Verification Design

Date: 2026-08-06

## Context

The latest real two-Profile run showed two independent failures:

- One logged-in Profile successfully placed copy in TikTok's comment editor, but V2 rejected it with `input_verification_failed` before the submit action.
- One logged-out Profile displayed `Log in to comment`; no editable comment target existed, so V2 correctly failed with `current_viewport_target_not_found`.
- V2 scroll currently sends a sampled number of 400–600 pixel wheel events and reports success without proving that TikTok changed videos.

The legacy execution module already solves video switching in `browser_video_switch.execute_verified_switches`. It sends bounded 120-delta wheel pulses and counts a switch only after a different stable video identity is observed.

## Goals

1. Define V2 scroll count as the number of successfully verified TikTok video switches.
2. Reuse the legacy verified video-switch implementation instead of duplicating it.
3. Verify input correctly for `input`, `textarea`, and `contenteditable` targets.
4. Preserve strict failure behavior: later actions do not execute after an unverified switch or input.
5. Keep existing saved strategies readable without a data migration.

## Non-goals

- Logging a TikTok Profile in automatically.
- Bypassing TikTok authentication or risk controls.
- Rewriting `browser_video_switch`.
- Adding a second scroll mode for arbitrary pixel scrolling.
- Changing readiness-element behavior or window tiling.

## Design

### Verified video switching

V2's scroll action calls `browser_video_switch.execute_verified_switches` directly.

The existing V2 fields map as follows:

- `direction`: passed unchanged as `up` or `down`.
- `count`: sampled once and passed as `requested`; it means completed video switches, not wheel events.
- `interval_seconds`: passed as the delay range between completed switches.
- `distance_pixels`: retained in stored payloads for backward compatibility but ignored at runtime.

The legacy function remains the single owner of:

- active feed-state capture;
- fixed 120-delta wheel pulses;
- mouse placement over the feed container;
- stable video-identity change detection;
- bounded pulse retries and timeouts.

V2 maps its successful result to safe action evidence containing:

- `requested_switches`;
- `completed_switches`;
- `wheel_events`;
- `direction`;
- `interval_seconds`.

Per-video fingerprints and raw page data are not stored in the V2 public result.

`VideoSwitchError.code` is preserved as the V2 action error code. Expected failures include `video_switch_timeout`, `video_switch_not_observed`, and `video_switch_state_capture_failed`. Strategy execution stops at that action and captures failure evidence using the existing V2 path.

### Input verification

V2 reads the target before and after typing.

- Native `input` and `textarea` elements use their `value`.
- Other editable elements use `innerText`, falling back to `textContent`.
- Verification normalizes CRLF, non-breaking spaces, and consecutive whitespace in both expected and observed text.
- Success requires the normalized expected text to appear after typing and its occurrence count to increase relative to the pre-input value.

This accepts DOM formatting changes introduced by `contenteditable` while preventing a pre-existing matching value from producing false success.

A logged-out Profile remains a Profile-state failure. If TikTok presents no editable comment target, V2 must not claim input or submission success. Authentication is an acceptance precondition, not an executor repair.

### Management UI

The scroll action editor changes terminology:

- `滚动次数范围` becomes `视频切换次数范围`.
- Help text states that one count means one verified video change.
- The pixel-distance field is no longer shown for new or edited V2 strategies.

Serialization keeps `distance_pixels` with a compatibility value of `[120, 120]`. Existing values such as `[400, 600]` remain loadable but are ignored during execution.

Run history shows completed switches and wheel-event count separately. This prevents a successful wheel dispatch from being mistaken for a successful video switch.

## Compatibility

- No SQLite migration.
- Existing V2 scroll actions remain valid.
- Existing strategy action order and IDs remain unchanged.
- Legacy execution continues calling the same `execute_verified_switches` function.
- V2 does not depend on legacy strategy schemas; it reuses only the verified switch primitive.

## Tests

1. V2 scroll passes sampled `count` as `requested` and ignores `distance_pixels`.
2. One requested switch is successful only when the reused function reports one completed switch.
3. Verified-switch error codes stop later V2 actions unchanged.
4. Stored action results distinguish `completed_switches` from `wheel_events`.
5. Native input verification succeeds only after value growth.
6. `contenteditable` verification accepts equivalent whitespace and line-break formatting.
7. Pre-existing matching text cannot satisfy verification.
8. UI labels count as video switches and hides pixel distance.
9. Existing saved 400–600 pixel strategies load and serialize compatibly.
10. Existing `browser_video_switch`, V2 Python, and Node test suites remain green.

## Real acceptance

Use two logged-in AdsPower test Profiles and one strategy containing wait, verified scroll, comment entry click, input, and submit click.

Acceptance requires:

- each Profile opens and tiles normally;
- requested switch count equals completed switch count;
- each counted switch corresponds to a stable changed video;
- both Profiles visibly receive input;
- input verification passes before submit;
- submit runs only after verified input;
- all Profile windows close after completion;
- a separately tested logged-out Profile fails without claiming a comment submission.
