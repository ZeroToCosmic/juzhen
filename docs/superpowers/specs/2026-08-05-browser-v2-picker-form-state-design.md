# Browser V2 Picker Form State Design

## Goal

Fix two picker UI regressions without adding APIs or changing picker backend behavior:

- Element-name input remains focusable and editable while picker status polls once per second.
- Selected AdsPower Profile remains selected and is locked while the picker is active.

## Root cause

`refreshActive()` polls picker status once per second and calls the global `render()` function. Every render currently:

1. Clears and recreates the picker candidate form, replacing the focused name input.
2. Clears and recreates the Profile selector.
3. Attempts to restore the Profile from `state.picker.profile_token`, but picker start/status responses intentionally do not return that token.

The input therefore loses focus every second, while the Profile selector falls back to its empty option.

## Approved design

### Local Profile state

Add one frontend-only state field for the picker Profile token.

- Update it when the user changes the picker Profile selector.
- Capture it before starting the picker.
- Use it when rebuilding Profile options; do not depend on picker API responses.
- Disable the picker Profile selector while picker status is active.
- Re-enable it after finish, cancel, failure, or another terminal status.
- Keep the last selected value after terminal completion so another picker run can reuse it.

The token remains an existing opaque public Profile handle. It is not persisted to storage and is not exposed in rendered text.

### Stable candidate form

Derive a stable selection key from picker fields already returned by the V2 API, preferring `actionable_ancestor_fingerprint`, then `original_fingerprint`, `unique_css`, and `relative_xpath`.

- If the current selection key matches the rendered candidate form, update status/buttons only. Do not clear or replace the form.
- If selection disappears, clear the candidate form and rendered key.
- If a different selection arrives, replace the old form and create a new blank naming form.
- After a successful save, clear the rendered selection locally while waiting for the next selection.

This preserves the exact input DOM node, focus, partially typed name, purpose, and kind during polling.

### Scope boundary

Change only:

- `gateway/static/browser_v2.js`
- `tests-js/browser-v2-ui.test.js`

No backend, database, API schema, AdsPower adapter, polling interval, picker overlay, or V1 module changes.

## Error behavior

- Failed picker status reads retain the current local Profile selection and form contents.
- A terminal picker status unlocks Profile selection.
- A new picker selection intentionally replaces an unfinished form because it represents a different target.
- Save failure keeps the same form and user input available for retry.

## Acceptance tests

1. Start picker with Profile A; start response omits `profile_token`; Profile A remains selected and disabled.
2. Poll the same selected element multiple times; the candidate form and name input node identities remain unchanged.
3. Typed name, purpose, and kind remain unchanged through polling.
4. A different selected element replaces the form with a new blank name.
5. Save failure preserves the form; save success clears it for the next selection.
6. Finish, cancel, failure, and terminal completion unlock the Profile selector without losing its last value.
7. Existing V2 frontend and picker tests continue passing.
