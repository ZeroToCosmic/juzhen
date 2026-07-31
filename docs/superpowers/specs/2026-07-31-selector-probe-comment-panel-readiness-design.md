# Selector Probe Comment Panel Readiness Design

Date: 2026-07-31

## Decision

Use the core minimal fix. Change only the probe path needed to stop premature
comment-panel capture and preserve its failure classification.

No HTTP endpoint, database schema, Redis format, management UI, worker policy,
or storage behavior changes.

## Problem

The probe currently treats the TikTok comment panel as ready when its visible
scope first resolves. TikTok may still be rendering Skeletons, the editable
textbox, submit control, or accessibility data. The probe then captures an
incomplete page or exits and immediately closes the AdsPower window.

The comment-panel transition also has only one attempt. State-transition
exceptions are converted to generic failures, which can hide the real cause or
incorrectly enter selector/LLM repair.

## Goals

- Never extract the final snapshot or run selector Dry-Run while the comment
  panel is still loading.
- Wait at most 60 seconds per attempt.
- Sample every two seconds and require three consecutive stable samples.
- Retry the complete comment-panel transition at most three times.
- Preserve a safe readiness failure code through validation and healing.
- Do not invoke LLM repair for page-readiness failures.
- Keep all existing external interfaces and persistence formats unchanged.

## Non-goals

- No new endpoint, table, Redis key, setting, or UI.
- No failed-run screenshot or new validation-record persistence.
- No 60-second manual debug hold.
- No new worker/store pause or recovery behavior.
- No guarantee that terminal readiness failure pauses only comment-dependent
  strategies; existing policy remains unchanged.
- No TikTok private API, network-response dependency, or fixed sleep as the
  primary readiness rule.
- No unrelated refactor.

The existing cleanup behavior remains: after the final failure, owned pages and
Profiles close immediately. The fix gives each attempt enough time to prove
readiness before that cleanup occurs.

## Changed Components

### `selector_probe/state_runner.py`

Add private comment-panel readiness sampling and fingerprint helpers. The
external state name remains `comment_panel_open`; `current_state` changes to
that value only after the gate passes.

The readiness sample checks:

1. panel shell is visible;
2. exactly one usable input container is visible;
3. exactly one editable textbox is visible;
4. exactly one submit control is visible;
5. no visible Skeleton or Spinner exists inside the panel;
6. panel does not report `aria-busy=true`;
7. the semantic fingerprint is unchanged for three consecutive samples.

A disabled submit control is valid before text entry.

The fingerprint contains only stable control facts:

- panel role and busy state;
- input role, accessible name, `data-e2e`, `aria-label`, and
  `contenteditable`;
- submit role, accessible name, `data-e2e`, `aria-label`, and disabled state;
- uniqueness counts for required controls.

Comment text, counts, avatars, timestamps, and list contents are excluded.

### `selector_probe/probe.py`

Increase the comment-panel transition from one attempt to three. Every failed
attempt reloads the feed, waits for existing feed readiness, reopens the
comment panel, and reruns the gate.

Final semantic snapshot extraction, candidate generation, and Dry-Run execute
only after the gate passes.

### `selector_probe/validator.py`

When state transition raises a safe probe error, preserve its code instead of
always replacing it with generic `required_state_failed`.

### `selector_probe/healing_runtime.py`

Classify readiness codes separately from selector failures. Return the real
readiness code to the probe and skip LLM self-correction for:

- `comment_panel_readiness_timeout`
- `comment_panel_snapshot_unstable`

Stable, fully loaded pages with missing elements continue through the existing
selector-failure path.

## Run Flow

For each existing Profile/round:

1. navigate or reload TikTok;
2. pass existing feed readiness;
3. click the comment entry;
4. wait for the panel shell;
5. sample readiness every two seconds;
6. require three consecutive stable fingerprints;
7. extract the final semantic snapshot;
8. generate candidates and run Dry-Run;
9. continue existing result and cleanup flow.

On readiness timeout, repeat steps 1-6. After three failed attempts, return the
real failure code and use existing cleanup.

## Error Rules

- Loading markers, `aria-busy`, changing fingerprints, and indeterminate
  control absence are readiness failures.
- A required control missing only after a stable loaded panel is a selector
  failure and may use the existing LLM repair loop.
- Cleanup failure must not overwrite an already captured readiness code.
- No raw DOM, comment text, Cookie, token, or accessibility tree is added to
  error output.

## Tests

### State runner

- Persistent Spinner never passes.
- Spinner disappearance plus three stable samples passes.
- Changing fingerprints reset the consecutive counter.
- Disabled submit remains acceptable.
- Dynamic comment-list changes do not alter the fingerprint.
- Timeout returns a readiness code.

### Validation and healing

- Safe readiness codes survive state validation.
- Readiness failures do not invoke LLM repair.
- Stable selector failures retain existing LLM behavior.
- Cleanup cannot replace the original readiness code.

### Probe integration

- Comment transition attempts at most three times.
- A retry reloads the feed and reopens the panel.
- Snapshot extraction and Dry-Run never run before readiness succeeds.
- Third readiness failure returns the real readiness code.
- Existing public API, persistence, publication, and cleanup tests remain
  unchanged.

## Acceptance Criteria

- Loading comment panel cannot be marked ready.
- Each attempt waits at most 60 seconds.
- Sampling interval is two seconds.
- Readiness requires three consecutive stable samples.
- Comment flow retries at most three times.
- Final snapshot and Dry-Run start only after readiness succeeds.
- Readiness failures never invoke LLM repair.
- Existing external interfaces and storage formats do not change.
- No worker/store/UI changes are included.
