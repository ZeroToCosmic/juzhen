# Selector Discovery and Contract Management Design

## Goal

Keep safe interactive discoveries visible even when selector Dry-Run fails, and let administrators edit or delete managed semantic contracts from the element directory.

## Approved behavior

- Discovery and contract validation are separate concerns.
- After each successful A11y snapshot, collect visible in-viewport interactive candidates using the existing discovery filter.
- A failed deterministic candidate search or Dry-Run must not discard collected discoveries.
- Run detail merges discoveries collected by healing runs with discoveries already stored in validation evidence.
- No new HTTP endpoint or database table.
- Administrators can edit an existing element's display name and semantic contract through the existing draft PATCH endpoint.
- Editing creates a new draft revision, clears stale draft validation, and leaves the active/LKG selector unchanged.
- Administrators can delete an unreferenced element through the existing DELETE endpoint after confirmation.
- Referenced elements cannot be deleted. The UI disables deletion and lists the dependent strategies/actions.
- Operators remain read-only.

## Minimal data flow

```text
A11y snapshot
  -> existing safe discovery filter
  -> runtime discovery buffer
  -> probe run details_json.discoveries
  -> existing run-detail response
  -> candidate list / Add to element directory
```

## UI behavior

Element detail adds `Edit contract` and `Delete element` actions for administrators. Edit reuses the current three-step wizard, prefilled from the saved contract. Delete uses a browser confirmation, sends the current revision, and returns to the refreshed directory on success.

When dependencies exist, the delete action is disabled and the detail shows strategy name/id plus action id/type. If the backend detects a new dependency or stale revision after rendering, the UI keeps the detail open and shows the safe error.

## Security and boundaries

- Discovery remains restricted to the existing role allowlist, stable-attribute allowlist, visibility checks, redaction, and 200-item limit.
- No raw DOM, prompt text, backend node id, credential, or unmasked Profile ID is exposed.
- Delete never cascades, unbinds, or pauses strategies.
- Existing optimistic revision checks and audit events remain authoritative.

## Acceptance

1. A healing run that reaches A11y extraction but ends in `zero_match` exposes safe discoveries.
2. Existing validation-evidence discoveries still appear and duplicates merge by fingerprint.
3. Editing preloads the complete semantic contract and PATCHes the current revision.
4. Edited elements become draft-pending-validation without replacing active/LKG selectors.
5. Unreferenced elements delete after confirmation.
6. Referenced elements show dependencies and cannot issue DELETE.
7. Administrator/operator permissions remain unchanged.
