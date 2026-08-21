# Selector Probe Profile Bulk Save Fix

Date: 2026-07-30
Status: Approved

## Goal

Allow an administrator to stage two or more dedicated AdsPower test Profile IDs
and save them atomically from the selector-probe settings page.

## Scope

- Replace the single Profile ID input with a multiline input.
- Add an action that imports the Profiles currently selected in the existing
  AdsPower window selector.
- Merge manual and imported IDs into one in-memory staged list.
- Trim, deduplicate, display masked values, and allow removal before saving.
- Send every staged ID in one existing `profile_changes.add` array.
- Normalize the target Origin by removing a trailing slash before validation.
- Preserve existing reason, confirmation, authorization, audit, revision, and
  atomic settings-write behavior.
- Keep staged IDs after validation or request failure so the user can retry.

No new API, database table, module split, or unrelated refactor is included.

## Error Handling

Show clear Chinese messages for an empty import, duplicate IDs, fewer than two
Profiles when enabling the probe, missing change reason, stale revision, and
save failure. Never display full saved Profile IDs.

## Verification

Add focused JavaScript tests for multiline input, selected-Profile import,
mixed-source deduplication, Origin normalization, one atomic request, and
retaining staged IDs after failure. Run relevant selector-probe backend tests to
confirm the existing list-based API remains compatible.
