# Selector Probe Control-First Comment Readiness Design

**Date:** 2026-07-31

## Context

The current comment-panel readiness sampler treats any visible Skeleton inside
the resolved comment panel as proof that the entire panel is not ready. TikTok
may continue lazy-loading the comment list after the comment composer is usable.
That makes a list Skeleton block selector extraction for the comment input and
submit button, causing `comment_panel_readiness_timeout` after three retries.

The probe only needs stable, actionable critical controls. It does not need the
entire comment list to finish loading.

## Goal

Use the critical comment controls as the readiness boundary. The panel is ready
when the comment input, editable textbox, and submit button are uniquely
visible, semantically correct, and stable for three consecutive samples.
Comment-list Skeletons remain diagnostic evidence but do not block readiness.

## Scope

Change only the existing comment-panel readiness gate and its tests.

Keep unchanged:

- the 60-second readiness deadline;
- three complete comment-transition attempts;
- page reload and state cleanup between attempts;
- the existing A11y extraction and Dry-Run after readiness;
- fail-closed publication behavior;
- existing API, UI, Redis, database, configuration, and worker interfaces.

## Readiness Flow

For each readiness sample:

1. Resolve one visible comment panel.
2. If the panel is missing, keep waiting.
3. If the resolved panel has `aria-busy="true"`, keep waiting.
4. Observe visible comment-list Skeleton or loading markers for diagnostics,
   but do not use them as a blocking condition.
5. Resolve exactly one visible comment input container.
6. Resolve exactly one visible editable textbox with A11y role `textbox`.
7. Resolve exactly one visible submit control with A11y role `button`.
8. Allow a disabled submit button because an empty comment normally disables
   submission.
9. Build the existing bounded fingerprint from the critical controls and stable
   attributes.
10. Require the same eligible fingerprint in three consecutive samples.
11. Continue into the existing A11y extraction and Dry-Run only after the gate
    passes.

The comment list and its dynamic contents are excluded from the fingerprint.

## Error Classification

- `comment_panel_readiness_timeout`: the panel never becomes visible, or remains
  `aria-busy="true"` until the deadline.
- `comment_panel_element_missing`: the panel is visible and not busy, but the
  required input, textbox, or submit control remains incomplete until the
  deadline.
- `comment_panel_snapshot_unstable`: all required controls become eligible, but
  their fingerprint cannot remain identical for three consecutive samples by
  the deadline.
- `probe_panel_check_failed`: an unexpected Playwright, locator, or A11y query
  failure prevents a safe decision.

Incomplete controls must keep waiting until the deadline. A stable fingerprint
of missing controls must not fail early.

## Retry and Safety Behavior

On failure, preserve the existing full retry cycle:

1. clean up the current comment state;
2. reload the TikTok feed;
3. wait for feed readiness;
4. reopen the comment panel;
5. run the readiness gate again.

After three failed transitions, stop the probe run and publish nothing. Existing
selectors and strategy state remain unchanged. LLM repair is not invoked for
these infrastructure/readiness failures.

The existing Dry-Run remains the second safety boundary. A control that appears
visible but is not actionable must fail Dry-Run and cannot be published.

## Tests

Add or update focused tests for:

- visible comment-list Skeleton plus stable critical controls passes;
- `aria-busy="true"` remains blocking;
- delayed input controls do not cause early failure;
- a disabled submit button remains valid;
- incomplete controls at the deadline produce
  `comment_panel_element_missing`;
- eligible but changing fingerprints produce
  `comment_panel_snapshot_unstable`;
- absent or perpetually busy panels produce
  `comment_panel_readiness_timeout`;
- Playwright/A11y sampling failures produce `probe_panel_check_failed`;
- Dry-Run failure still prevents publication;
- existing retry, cleanup, observe-only, and publication regression tests pass.

## Non-Goals

- waiting for every TikTok comment to load;
- geometry-based Skeleton overlap detection;
- new screenshots or evidence storage;
- new management API or UI;
- changing Profile sequencing;
- changing automatic pause, resume, publish, or Redis contracts.
