# Verified Video-Switch Scrolling Design

Date: 2026-07-25

## Goal

Change `scroll_up` and `scroll_down` from counting emitted wheel events to
counting verified TikTok video switches.

If a strategy requests 5 downward switches, the action succeeds only after the
visible active video has changed 5 times. Emitting a wheel event without a
visible video change does not increase the completed count.

## Confirmed product decisions

- Use state-verified wheel scrolling, not direct `scrollTop` assignment and not
  keyboard navigation.
- Existing saved minimum/maximum values remain unchanged.
- Existing values are not divided, converted, or otherwise migrated.
- The user will edit the saved range to the desired number of video switches.
- `scroll_up` and `scroll_down` use the same verification model with opposite
  wheel direction.

## Configuration semantics

The persisted action shape remains compatible:

```json
{
  "type": "scroll_down",
  "params": {
    "total_count": [3, 5],
    "interval_seconds": [1.0, 3.0]
  }
}
```

New meaning:

- `total_count`: minimum and maximum successful video switches.
- `interval_seconds`: random pause after one verified switch and before
  attempting the next switch.
- The internal wheel delta remains `-120` for upward and `+120` for downward.
- The hidden legacy `burst_count` value is not used to define success and is not
  restored to the UI.

The UI labels become:

- `最少切换视频数`
- `最多切换视频数`
- `最小切换间隔秒数`
- `最大切换间隔秒数`

## Runtime algorithm

The target switch count is sampled once at the start of the action.

For each required switch:

1. Resolve the current active page through the existing page-lifecycle manager.
2. Find the scroll target:
   - prefer TikTok's visible `#column-list-container`;
   - otherwise use the nearest vertically scrollable ancestor under the viewport
     center;
   - fail clearly if no usable target exists.
3. Move the Playwright/Ghost Cursor pointer to the center of that scroll target.
4. Capture the current active-video fingerprint.
5. Emit one `wheel(0, ±120)` pulse.
6. Poll the page for a new active-video fingerprint and stable snap position.
7. If no switch is observed, emit another pulse and repeat without incrementing
   the completed count.
8. Increment the completed count only when a different active video is stable.
9. Apply the configured random interval before attempting the next switch.

The number of pulses allowed for one switch scales with the viewport:

```text
ceil(scroll_container_height / 120) + 4
```

It is bounded to a safe range of 4–24 pulses. The per-switch observation timeout
is 5 seconds, excluding the configured pause between successful switches.

## Active-video identity

The active item is the visible feed item crossing the center line of the scroll
container.

The fingerprint uses, in priority order:

1. TikTok video ID parsed from a descendant `/video/<id>` link.
2. The active feed article's stable `id`.
3. A deterministic fallback composed from stable element attributes and its
   position within the active feed.

User text, captions, account names, cookies, and credentials are never included
in the fingerprint or logs.

A switch is accepted only when:

- the new fingerprint differs from the previous fingerprint;
- the new item occupies the scroll-container center;
- the container has reached a stable snap position in two consecutive polls.

## Results and diagnostics

Successful action result:

```json
{
  "status": "ok",
  "requested_switches": 5,
  "completed_switches": 5,
  "wheel_events": 39,
  "switches": [
    {
      "index": 1,
      "direction": "down",
      "from": "video:masked-id",
      "to": "video:masked-id",
      "wheel_events": 8
    }
  ]
}
```

For backward compatibility, the existing `count` result field equals
`completed_switches`. The configured result field `distance` remains `120`.

Diagnostics record only masked/hash-derived fingerprints, counts, direction,
timings, recovery outcome, and safe origins.

## Failure handling

The action fails instead of reporting false success when:

- no usable scrolling container is found;
- the current active video cannot be identified;
- the maximum pulse count or observation timeout is reached without a switch;
- the feed is blocked at its beginning/end;
- the page remains unavailable after the existing one-time page replacement
  recovery.

The staged error carries:

- requested and completed switch counts;
- emitted wheel-event count;
- failed switch index;
- safe failure code such as `scroll_target_not_found` or
  `video_switch_not_observed`;
- ordered page-recovery events.

If the page closes during a pending switch, the existing lifecycle recovery
rebinds to the replacement page. The pending switch is not counted until a new
active video is verified.

## Tests

TDD coverage must prove:

- one requested count means one verified video change, not one wheel call;
- several wheel pulses may be required for one switch;
- ignored wheel pulses never increase the count;
- exactly N successful switches produce `completed_switches == N`;
- upward and downward directions work;
- the target count is sampled once;
- configured pause happens between verified switches;
- viewport-scaled pulse limits are enforced;
- a stable new item is required to avoid double-counting transition frames;
- page replacement resumes the pending switch without double-counting;
- failure returns partial counts and ordered recovery diagnostics;
- existing saved ranges are retained without conversion;
- UI labels and serialization use video-switch terminology;
- existing click, keyboard, pause, Ghost Cursor, and persistence behavior remain
  unchanged.

## Live acceptance

With explicitly authorized test profiles:

1. Set the range to `[3, 3]`.
2. Run one downward-scroll action.
3. Confirm exactly three active-video changes.
4. Confirm `completed_switches == 3`.
5. Confirm `wheel_events >= 3` and may be greater than three.
6. Repeat upward from a non-initial feed position.

No live AdsPower profile is started or mutated without explicit test
authorization.

## Out of scope

This change does not repair the currently invalid TikTok comment-entry,
comment-input, and submit selectors. Those selectors presently match zero
elements and require a separate resilient-element design.

This change also does not make Playwright's synthetic cursor move the Windows
hardware pointer. Visible cursor behavior remains a separate product choice.
