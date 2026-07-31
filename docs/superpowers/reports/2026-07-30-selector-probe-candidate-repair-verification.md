# Selector Probe Candidate Repair Verification

Date: 2026-07-30  
Result: Passed

## Automated tests

Focused suites:

```text
tests/test_selector_probe_candidates.py
tests/test_selector_probe_contracts.py
tests/test_selector_probe_healing_runtime.py
tests/test_selector_probe_validator.py
tests/test_browser_element_resolver.py
tests/test_settings_store.py
```

Result: 176 passed.

Final selector/browser regression:

```powershell
.\.venv\Scripts\python.exe -m pytest tests `
  -k "selector_probe or browser_element" -q -p no:cacheprovider
```

Result: 804 passed, 1 skipped, 1490 deselected.

## Live observe acceptance

- Run ID: 16
- Terminal status: `completed`
- Dedicated Profiles observed: 2
- Fresh rounds per Profile: 2
- Validation records: 8
- Passed records: 8
- Failed records: 0
- Lease released: yes
- Probe-owned Profiles after cleanup: both inactive

All four Profile/round combinations produced identical primary candidates:

| Element | Scope | Primary candidate |
|---|---|---|
| 评论入口 | `active_video` | `data-e2e=comment-icon` |
| 评论输入框 | `visible_comment_panel` | ancestor `data-e2e=comment-input`, descendant `contenteditable=true`, Role `textbox` |
| 评论提交按钮 | `visible_comment_panel` | `data-e2e=comment-post` |

The probe opened the comment panel read-only. It did not type comment text or
click the submit button.

## Publication and configuration invariants

- Selector versions created for run 16: 0
- `published_version_after`: empty
- Redis publication: none
- Strategy-gate mutation: none
- Stored `selector_probe.enabled`: `true`
- Stored `selector_probe.observe_only`: `false`
- Stored `selector_probe.rollout_mode`: `publish`
- Config remained byte-identical to the pre-live backup.

## Backups

- `config.pre-live-observe-20260730.json`
- `data/selector-probe.pre-live-observe-20260730.db`

Config SHA-256:

```text
FD6C6C91BD338BA930CAA2D248138A88099078E4BFD29A58327A2E70E320A4C2
```

The database differs from its backup only because observe runs and their
sanitized evidence were appended.

## Changed implementation

- `browser_element_resolver.py`
- `config.json`
- `selector_probe/candidates.py`
- `selector_probe/contracts.py`
- `selector_probe/healing_runtime.py`
- `selector_probe/probe.py`
- `selector_probe/state_runner.py`
- `selector_probe/validator.py`
- focused regression tests under `tests/`

This workspace has no Git repository metadata, so no commit was created.
