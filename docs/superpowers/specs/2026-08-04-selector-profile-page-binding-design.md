# Selector Profile/Page Binding Repair Design

Date: 2026-08-04
Status: Approved

## Problem

The probe starts two AdsPower Profiles and creates one Playwright Page for each,
but healing discovery operates only on the primary Page. If discovery fails
before full validation, the second Page stays blank. The session layer also
accepts duplicate CDP endpoints and allows repeated Page creation for one
Profile, so two probe Pages can attach to one browser window.

## Design

- Reject two Profile handles that resolve to the same canonical CDP endpoint
  with `profile_cdp_collision`.
- Permit one probe-created Page per Profile for one session manager instance.
- Record a sanitized `profile_page_binding` progress stage after Page creation.
- In healing mode, create both Pages, then require `feed_ready` on both before
  deterministic discovery begins.
- Reuse already-ready feed state for the first discovery capture; reload only
  when existing retry rules require it.
- Close only `ProbePageHandle(created_by_probe=True)` Pages and stop only
  Profiles started by the probe.

## Acceptance Criteria

1. Duplicate CDP endpoints fail before any Page is created.
2. A second Page request for one Profile fails with `probe_page_duplicate`.
3. Both Profile Pages reach `feed_ready` before deterministic discovery.
4. A failure on primary discovery cannot leave the second probe Page blank.
5. Progress contains one passed `profile_page_binding` per Profile.
6. Existing cleanup ownership, APIs, Redis, database, and strategy gates remain unchanged.

