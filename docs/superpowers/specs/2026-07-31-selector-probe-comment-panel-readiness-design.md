# Selector Probe Comment Panel Readiness Design

Date: 2026-07-31

## Problem

The probe currently treats the TikTok comment panel as open as soon as the
visible comment-panel scope resolves. That scope is based on a usable
`[data-e2e="comment-input"]` and its ancestor section. It does not prove that
the panel has finished loading or that the accessibility representation is
stable.

The panel transition has one attempt. A partially rendered panel can therefore
move directly into element inspection. Any later infrastructure-class failure
exits the healing runtime, whose unconditional cleanup closes probe-owned pages
and AdsPower Profiles. The latest observed failure recorded zero validation
records, leaving insufficient evidence for diagnosis.

## Goals

- Do not capture or validate comment-panel elements while the panel is loading.
- Require semantic stability, not a fixed delay.
- Retry the complete comment-panel transition up to three times.
- Preserve a failed validation record and screenshot when no valid capture is
  possible.
- Trigger LLM repair only after a stable page proves that a selector is invalid.
- Keep the previous stable selector version on readiness failure.
- Pause only strategies that depend on comment-panel elements.
- Keep the existing HTTP API, database schema, Redis publication format, page
  states, and management entry points.
- Preserve the existing requirement that two Profiles and two rounds per
  Profile must pass before publication or automatic recovery.

## Non-goals

- No TikTok private API or network-response dependency.
- No fixed sleep as the primary readiness rule.
- No new management API, database table, Redis key format, or UI setting.
- No full DOM, comment text, Cookie, token, or raw accessibility-tree storage
  in readiness diagnostics.
- No unrelated selector-registry or strategy-runtime refactor.

## Architecture

The implementation remains inside existing modules.

### `selector_probe/state_runner.py`

Add private readiness helpers:

- `_wait_for_comment_panel_ready`
- `_comment_panel_readiness_sample`
- `_comment_panel_fingerprint`

The existing external state name `comment_panel_open` remains unchanged.
`current_state` may become `comment_panel_open` only after the readiness gate
passes.

The helpers reuse existing Playwright locators, scope resolution, semantic
snapshot extraction, loading markers, injected clock, sleep function, and
timeout configuration. No new public service or project-level interface is
introduced.

### `selector_probe/probe.py`

Change the comment-panel transition from one attempt to three attempts.
Each retry performs a full reset:

1. preserve bounded failure evidence;
2. capture a redacted screenshot;
3. reload the TikTok feed;
4. wait for feed readiness;
5. reopen the comment panel;
6. rerun the semantic readiness gate.

The final semantic snapshot, candidate generation, and Dry-Run occur only after
the gate passes.

### `selector_probe/healing_runtime.py`

Preserve failure classification and bounded readiness evidence instead of
collapsing every unexpected validation failure into a reasonless
`infrastructure_unavailable` result.

For a manual run, preserve the failed probe window for 60 seconds after evidence
capture. Scheduled runs close after evidence capture. Both paths still perform
owned-page and Profile cleanup; no Profile may remain active after the run.
The existing management request type distinguishes manual from scheduled runs,
so no new UI switch or endpoint is required.

## Comment Panel Readiness Gate

The gate has a 60-second maximum duration and samples every two seconds. It
requires three consecutive stable samples.

A sample records only bounded facts about these targets:

- comment input container;
- editable textbox descendant;
- comment submit button;
- containing comment-panel section;
- loading markers scoped to the panel.

### Required conditions

1. The comment panel shell is visible.
2. Exactly one comment input container is visible and uncovered.
3. Exactly one editable textbox descendant is visible.
4. Exactly one comment submit button is visible.
5. The submit button may be disabled. Disabled is expected before text entry.
6. No visible Skeleton or Spinner exists inside the panel.
7. The panel does not report `aria-busy=true`.
8. The key semantic fingerprint is unchanged across three consecutive samples.

### Semantic fingerprint

The fingerprint includes only stable control facts:

- panel role and busy state;
- input role, accessible name, `data-e2e`, `aria-label`, and
  `contenteditable`;
- submit role, accessible name, `data-e2e`, `aria-label`, and disabled state;
- presence and uniqueness counts for the required controls.

Comment text, comment count, avatars, timestamps, and list contents are excluded.
Normal comment-list updates must not reset the stability counter.

Any failed required condition resets the consecutive-stability counter.

### Stable absence

When the panel shell is stable, loading markers are absent, and a required
control remains absent across three consecutive samples, the result is
`comment_panel_element_missing`. This proves a selector or semantic contract
problem and may enter the existing LLM repair flow.

When loading markers remain, `aria-busy` remains true, the shell is unstable, or
the sample cannot safely determine absence, the result is a readiness failure
and must not enter LLM repair.

## Run Flow

For each Profile and each round:

1. Navigate or reload the TikTok feed.
2. Pass existing feed readiness checks.
3. locate and click the comment entry.
4. Wait for the comment-panel shell.
5. Sample readiness every two seconds.
6. Require three consecutive stable semantic fingerprints.
7. Extract the final full semantic snapshot.
8. Generate candidates.
9. Dry-Run managed elements.
10. Save validation evidence.
11. Close the comment panel.

No success state may be recorded unless the semantic snapshot and Dry-Run
evidence have been saved.

## Retry and Failure Policy

### Readiness failures

Codes:

- `comment_panel_readiness_timeout`
- `comment_panel_snapshot_unstable`

These are infrastructure/page-readiness failures. They do not trigger LLM
repair. The probe retries the complete panel flow up to three times.

After the third failure:

- record a failed validation row using the existing validation table;
- save a redacted screenshot and bounded readiness evidence;
- retain the previous stable selector version;
- publish no new selector version;
- pause only strategies whose dependencies require
  `visible_comment_panel` elements;
- leave unrelated strategies running;
- create the existing alert and configurable Webhook event.

### Selector failures

Code:

- `comment_panel_element_missing`

This code is valid only after stable absence has been observed. It enters the
existing feedback-loop LLM repair flow and selector retry policy.

### Cleanup

Success always closes probe-owned pages and Profiles.

Failure captures evidence before cleanup:

- manual run: hold failed window for 60 seconds, then clean up;
- scheduled run: clean up immediately after evidence capture.

Cleanup failure remains a separate failure and must not overwrite the original
readiness or selector code.

## Evidence

Use existing `probe_runs.details_json`,
`selector_validation_runs.evidence_json`, and progress records.

A bounded readiness failure includes:

```json
{
  "failure_code": "comment_panel_readiness_timeout",
  "attempt": 3,
  "elapsed_seconds": 60,
  "input_visible": true,
  "textbox_visible": true,
  "submit_visible": true,
  "submit_disabled": true,
  "loading_marker": "spinner",
  "aria_busy": false,
  "stable_samples": 1,
  "required_samples": 3,
  "fingerprint_hash": "sha256-value",
  "screenshot_path": "redacted-evidence-name.png"
}
```

Evidence limits:

- hashes instead of raw DOM;
- no comment text or user data;
- Profile IDs remain masked;
- screenshot uses existing redaction and path validation;
- sample history is bounded to the latest diagnostic samples;
- at least one failed validation record must exist when capture fails.

## Strategy Isolation

Readiness failure covers aliases whose required state is
`comment_panel_open` or whose scope is `visible_comment_panel`. Dependency
resolution uses the existing strategy-dependency model.

Only strategies depending on covered aliases are paused. Feed-only and unrelated
strategies remain eligible. The last stable selector mapping remains active.

Automatic recovery continues to require:

- at least two dedicated test Profiles;
- both rounds passing for every Profile;
- identical validated candidate results across rounds;
- successful atomic publication;
- no active manual pause.

## Tests

### Unit tests

1. Visible panel with persistent Spinner never passes.
2. Spinner disappearance followed by three stable samples passes.
3. Two changing fingerprints followed by three identical fingerprints pass on
   the fifth eligible sample.
4. A disabled submit button remains acceptable.
5. Any failed condition resets the stability counter.
6. Dynamic comment-list content does not affect the fingerprint.
7. Stable absence of input or submit returns
   `comment_panel_element_missing`.
8. Unstable or busy absence returns a readiness failure.
9. Timeout evidence contains bounded fields and no raw DOM.

### Probe integration tests

1. Comment transition retries exactly three times.
2. Each retry reloads, waits for feed readiness, and reopens the panel.
3. Snapshot extraction and Dry-Run never execute before readiness passes.
4. Third readiness failure records a failed validation and screenshot.
5. A capture failure cannot finish with `validation_records=0`.
6. Readiness failures do not invoke the LLM.
7. Stable selector failures invoke the existing repair loop.
8. Manual failures hold before cleanup; scheduled failures do not.
9. Original failure code survives cleanup.
10. Only comment-dependent strategies are paused.

### Regression and live verification

1. Run selector-probe and browser-element test suites.
2. Run an observe-mode live probe against both dedicated AdsPower Profiles.
3. Verify two rounds per Profile.
4. Confirm final semantic snapshots contain the input and submit controls.
5. Confirm Dry-Run succeeds for the comment entry, input, and submit button.
6. Confirm no selector publication or strategy mutation occurs in observe mode.
7. Confirm both AdsPower Profiles return to `inactive`.

## Acceptance Criteria

- A loading comment panel cannot be marked ready.
- Readiness requires three stable samples at two-second intervals.
- Comment loading may wait up to 60 seconds per attempt.
- The comment transition has three full attempts.
- No final snapshot or Dry-Run occurs before readiness.
- Every failed run has a validation record, safe failure code, and screenshot.
- Page-readiness failures do not trigger LLM repair.
- Stable selector failures retain the existing LLM repair behavior.
- The previous selector version remains active on failure.
- Only comment-dependent strategies pause after terminal readiness failure.
- Two Profiles, two consistent rounds, and atomic publication remain mandatory
  for automatic recovery.
- Manual failures retain the window for 60 seconds; scheduled runs clean up
  after evidence capture.
- All owned pages close and both test Profiles become inactive when the run
  finishes.
