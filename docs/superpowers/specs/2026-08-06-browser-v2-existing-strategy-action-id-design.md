# Browser V2 Existing Strategy Action ID Design

## Problem

The Browser V2 UI starts its in-memory action counter at zero on each page load. When a saved strategy already contains action IDs such as `action_1` and `action_2`, adding another action can reuse `action_1`. The backend correctly rejects the strategy because action IDs must be unique, but the UI shows only the generic business-validation message.

## Scope

- Fix action ID allocation when adding actions to an existing strategy.
- Preserve every existing action ID, action order, and action parameter.
- Keep the backend uniqueness validation unchanged.
- Do not change the API, database, executor, strategy schema, or page layout.

## Design

Add a small action-ID allocator in `gateway/static/browser_v2.js`.

For each new action:

1. Read IDs already present in `state.draft.definition.actions`.
2. Increment the existing page-level sequence.
3. Produce `action_N` only when that ID is not already used.
4. Pass the unused ID to `actionTemplate()`.

This allocation happens at insertion time. Saved actions are never renumbered, and the backend remains the final uniqueness guard.

## Error Handling

No new error response is required. Avoiding duplicate IDs prevents this known validation failure. Other invalid strategy fields continue to return the existing validation response.

## Verification

Add a JavaScript regression test that starts with an editable strategy containing `action_1` and `action_2`, then adds two actions and verifies:

- new IDs are `action_3` and `action_4`;
- all action IDs are unique;
- existing IDs remain unchanged.

Run the Browser V2 UI JavaScript tests and the full V2 regression suite.
