# Selector Probe Candidate Repair Implementation Plan

Status: Completed

## Completed work

- [x] Generate deterministic candidates from safe stable anchors even when
  accessible Name is empty.
- [x] Support bounded ancestor/descendant input anchors.
- [x] Rank the constrained `comment-input` locator before generic
  `contenteditable`.
- [x] Add `Publish` to the submit-name locale map.
- [x] Keep production resolution strict while allowing inspect-only validation
  of unique visible disabled/covered controls.
- [x] Correct the three canonical element scopes in `config.json`.
- [x] Preserve safe deterministic failure codes.
- [x] Start every validation round with full navigation and semantic readiness.
- [x] Avoid toggling an already-open comment panel; wait up to 15 seconds after
  one open click.
- [x] Fall back to probe-window cleanup when Escape cannot close the panel.
- [x] Generate and Dry-Run probe candidates during observe mode and expose
  recommended paths in run details.
- [x] Run focused and complete selector/browser regression suites.
- [x] Back up config and selector-probe database.
- [x] Complete read-only live acceptance on two dedicated Profiles.

## Live acceptance invariants

- Run: 16
- Result: `completed`
- Profiles: 2
- Rounds per Profile: 2
- Validation records: 8/8 passed
- Selector versions created: 0
- Redis publication: none
- Stored settings after acceptance: `rollout_mode=publish`,
  `observe_only=false`
- Probe-owned Profiles after cleanup: inactive
