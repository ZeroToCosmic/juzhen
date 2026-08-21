# TikTok Wheel Diagnostic Design

Date: 2026-07-27

## Goal

Distinguish why Playwright-dispatched TikTok wheel pulses produce zero verified
video switches:

1. the wheel event is not delivered to the feed;
2. the feed moves but does not snap to another video;
3. the DOM/article changes but the active-video identity observer misses it.

The diagnostic must not change normal scroll behavior or authorize comment
interaction.

## Authorized live scope

- Run exactly one diagnostic execution on `***xcto`.
- Run exactly one diagnostic execution on `***xctm`.
- Each execution requests one downward verified video switch.
- One execution may emit the existing bounded sequence of `+120` wheel pulses;
  it is not retried.
- Do not execute upward scrolling.
- Do not locate, move to, click, type into, or submit any comment element.
- Do not inspect, enumerate, start, stop, or control another AdsPower profile.
- Start each authorized profile at most once and stop it at most once, then
  confirm `Inactive` through read-only polling.

## Opt-in instrumentation

Normal strategy execution remains unchanged. Diagnostic collection is enabled
only when the caller explicitly passes `diagnostic=True` to the verified-switch
runtime.

For each dispatched pulse, collect only:

- pulse index and fixed numeric delta;
- whether a DOM `wheel` event was observed;
- whether the event target was the feed container or its descendant;
- whether the event was default-prevented;
- child-list mutation count inside the feed;
- observation poll count;
- whether any different identity candidate appeared;
- before/after numeric `container.scrollTop`;
- before/after numeric `window.scrollY`;
- before/after center-article top, bottom, and center-offset geometry;
- before/after identity-source enum: `video_id`, `article_id`, or `fallback`;
- before/after twelve-character identity hash.

Never collect or expose:

- raw Profile IDs;
- video IDs or article IDs;
- captions, usernames, comments, or other page text;
- URLs or origins;
- selectors, XPath, HTML, DOM attributes, cookies, credentials, or API keys.

## Listener lifecycle

Before every pulse:

1. resolve the same visible feed container and pointer center used by the
   production switch;
2. install a capture-phase wheel listener and a child-list mutation observer;
3. store state under a fixed private page key that contains no user data.

After the pulse observation window, on success, timeout, page error, or task
cancellation:

1. read the numeric/boolean counters;
2. disconnect the observer;
3. remove the wheel listener;
4. delete the private page key.

Cleanup runs in `finally`. No listener, observer, promise, timer, or background
task may survive a pulse.

## Runtime and result contract

- The existing `±120`, 450-millisecond pulse window, eight-second switch
  deadline, and viewport-derived `4..24` pulse bound remain unchanged.
- Diagnostics do not alter completed-switch or wheel-event counts.
- A diagnostic failure still uses `video_switch_not_observed` or the existing
  bounded timeout code.
- Safe pulse diagnostics are attached to both success results and
  `VideoSwitchError`.
- Existing non-diagnostic callers receive no pulse-diagnostic field and incur
  no page listener/observer work.

## Interpretation

- `wheel_seen == false`, or `target_in_container == false`, with no scroll,
  geometry, or mutation change: wheel was not delivered to the feed.
- Wheel observed in the feed and geometry/scroll changes, but no stable
  identity transition: the feed moved but did not snap.
- Geometry or mutations prove article replacement while identity source/hash
  remains unchanged: the active-video identity observer missed movement.
- No wheel/geometry/mutation evidence is insufficient to claim a TikTok
  virtualization defect.

## Automated tests

TDD must prove:

- diagnostics are absent when opt-in is false;
- listener and observer install before wheel dispatch;
- cleanup occurs after success, ordinary failure, timeout, and cancellation;
- listener metrics are boolean/nonnegative numeric values only;
- raw identities are hashed before being stored;
- wheel counts and switch counts are identical with diagnostics on and off;
- diagnostic collection failure cannot dispatch an extra wheel or leave page
  state behind;
- errors carry safe partial diagnostics for every completed pulse.

## Live report and cleanup

Record only masked Profile labels, execution count, requested/completed
switches, wheel count, safe pulse metrics, safe hashes, and the supported
diagnosis. Validate configuration is unchanged, every JSONL line parses, no
complete authorized Profile ID occurs in logs/reports, and both profiles are
`Inactive`.

