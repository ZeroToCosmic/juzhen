# Selector Live Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use test-driven, surgical changes and preserve unrelated dirty-worktree edits.

## Success criteria

- Admin can start picker with masked configured Profile reference and page state.
- Browser stays open while session is active; clicks select without causing TikTok actions.
- Polling shows up to 20 safe selections; confirm/cancel terminates and cleans resources.
- Optional position hint round-trips through element contracts and affects ranking only.
- Existing selector-probe Python and JavaScript tests remain green.

## Tasks

1. Add failing route/session/sanitization tests.
2. Implement `selector_probe/picker.py`: Redis state, shared lease, AdsPower/CDP runner, safe injection, cleanup.
3. Add four blueprint routes with Profile-ref resolution and role/ownership checks.
4. Add optional contract `position_hint` validation and weak candidate score bonus.
5. Add picker dialog markup, CSS, polling controller, selection list, confirm/cancel, and wizard handoff.
6. Run focused tests, then complete selector-probe Python and JS suites.
