# Verified Scroll and Resilient Locators Verification

Date: 2026-07-27  
Task: Task 8 — Canonical Persistence, Restart, and UI Integration Verification  
Status: DONE  
Commits: none

## Scope and change inventory

Task 8 added acceptance coverage in:

- `tests/test_settings_store.py`
  - canonical locator order/scope and `[30, 50]` save/load round-trip;
  - fresh-process restart probe importing `load_settings`.
- `tests/test_app.py`
  - canonical Flask element/strategy PUT, new app instance, GET, and persisted-file round-trip.
- `tests-js/browser-strategy-ui.test.js`
  - stateful canonical server/UI save/refresh round-trip.
- `tests/test_settings_routes.py`
  - five pre-existing v2/raw-string assertions advanced to the binding v3 canonical response contract after the supported full suite exposed them.

No production file changed. Hash comparisons against the pre-task baseline remain identical for `gateway/app.py`, `gateway/settings_store.py`, `gateway/static/browser_strategy_ui.js`, `browser_element_schema.py`, and `browser_strategy_config.py`.

Git was not initialized or used.

## Persistence and restart evidence

Initial persistence state, run before any compatibility fix:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_settings_store.py::test_locator_order_scope_and_scroll_range_survive_reload -p no:cacheprovider -q -W error
```

Result: `1 passed in 0.12s`.

Evidence:

- `strategy_schema_version` remains `3`;
- `action_elements` retains `active_video` scope;
- ordered locator candidates retain attribute-primary then exact raw-XPath fallback;
- scroll `total_count` remains numerically `[30, 50]`.

Because the initial persistence test passed, a fresh-process probe was added. Its first run exposed only a Windows test-transport encoding mismatch: the child imported and loaded successfully with exit code 0, but emitted non-ASCII JSON using the local Windows code page while the parent forced UTF-8. The probe was changed to ASCII-safe JSON escapes; production was not changed.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_settings_store.py::test_canonical_browser_state_survives_fresh_process_restart -p no:cacheprovider -q -W error
```

Result after the test-only correction: `1 passed in 0.16s`.

The child is a new Python process, imports `load_settings`, reads the temporary config, emits canonical JSON, and proves locator order/scope, action aliases/types, and `[30, 50]` survive restart.

## API and UI-controller round-trip evidence

Flask API round-trip initial state:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py::test_canonical_browser_api_persist_and_reload_preserves_locator_and_strategy_state -p no:cacheprovider -q -W error
```

Result: `1 passed in 3.91s`.

The test PUTs canonical ordered locators and a strategy, consumes the canonical server responses, creates a new Flask application client, GETs both resources, and verifies persisted-file equality, alias retention, scope/order retention, and `[30, 50]`.

Node UI round-trip initial state:

```powershell
node --test --test-name-pattern="canonical locator and strategy state round-trips through save and refresh" tests-js/browser-strategy-ui.test.js
```

Result: `1` test, `1` pass, `0` fail, duration `96.641ms`.

The test:

- loads canonical elements and strategies;
- changes `commentEntry` scope from `active_video` to `visible_comment_panel`;
- moves the XPath fallback before the role candidate;
- saves elements and consumes the canonical element response;
- saves the strategy and consumes the canonical strategy response;
- creates a fresh controller to represent refresh;
- verifies locator IDs remain `["entry-xpath", "entry-role"]`;
- verifies `[30, 50]` is not converted;
- verifies element aliases remain `commentEntry`, `commentInput`, `commentSubmit`;
- verifies action types remain `click`, `scroll_down`, `keyboard_input`, `click`.

No production fix was needed because both initial acceptance tests passed.

## Migration and stale-test RED/GREEN evidence

The first supported full Python run reported:

```text
5 failed, 1341 passed in 407.61s (0:06:47)
```

All five failures were in pre-existing `tests/test_settings_routes.py` assertions that expected:

- successful element writes to persist and return raw XPath strings; or
- first legacy migration to persist schema version `2`.

Task 1's binding schema-v3 brief requires successful canonical writes to use version `3`, while legacy strings migrate to a `page`-scoped single XPath fallback with the selector text preserved exactly. The current schema tests already prove lossless, whitespace-preserving, idempotent migration.

Exact five-test RED:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_settings_routes.py::test_browser_local_save_routes_preserve_existing_config tests/test_settings_routes.py::test_strategy_resources_save_survive_new_app_instance tests/test_settings_routes.py::test_element_deletion_replaces_mapping_and_preserves_unrelated_settings tests/test_settings_routes.py::test_element_rename_rewrites_strategy_reference_atomically tests/test_settings_routes.py::test_first_resource_read_persists_legacy_migration_once -p no:cacheprovider -q -W error
```

Result: `5 failed in 9.91s`.

Smallest correction: update only those assertions through `_assert_migrated_xpath_elements`, which verifies:

- aliases are unchanged;
- scope is `page`;
- exactly one XPath candidate exists;
- a stable `locator-...` ID exists;
- XPath value is byte-for-byte equal to the submitted string;
- candidate remains enabled and marked as fallback;
- successful persisted schema version is `3`.

Exact five-test GREEN with the same command: `5 passed in 9.40s`.

No production behavior was changed to satisfy obsolete v2 expectations.

## Exact focused verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_settings_store.py tests/test_browser_strategy_config.py tests/test_app.py -k "persist or reload or restart or migrate" -p no:cacheprovider -q -W error
```

Final result: `19 passed, 370 deselected in 16.20s`.

```powershell
node --test tests-js/browser-strategy-ui.test.js
```

Final result: `40` tests, `40` passed, `0` failed, duration `114.6328ms`.

## Exact full verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q
```

Final result: `1346 passed in 409.75s (0:06:49)`.

```powershell
npm.cmd run test:node
```

Result: `127` tests, `127` passed, `0` failed, duration `575.2172ms`.

```powershell
.\.venv\Scripts\python.exe -m py_compile browser_element_schema.py browser_element_resolver.py browser_video_switch.py browser_actions.py browser_strategy_config.py browser_strategy_runtime.py browser_page_lifecycle.py gateway\app.py
```

Result: exit code `0`, no output.

## Local dashboard inspection

An isolated local dashboard was launched at:

```text
http://127.0.0.1:5018/?panel=strategies
```

It used only:

```text
C:\tmp\verified-scroll-task8-ui-config.json
```

HTTP response: `200`, page length `107366` characters. The pre-existing unrelated service on port `5000` was not stopped or modified.

Live-page structural checks confirmed:

- locator scope control exists;
- ordered locator list exists;
- add-locator control exists;
- TikTok template control exists;
- per-window result table exists with `窗口 / 元素 / 状态 / 代码 / 诊断`;
- advanced XPath copy exists;
- legacy strategy JSON and legacy XPath JSON editors are absent.

Parent-side in-app-browser inspection confirmed:

- the locator editor rendered functional alias and scope fields;
- scope choices were page, current video, and visible comment panel;
- ordered candidates supported attribute, CSS, role, and advanced XPath types;
- attribute name/value fields and enabled toggle rendered;
- no placeholder-only controls appeared;
- the TikTok template button opened a native confirmation;
- after confirmation, the draft populated `评论入口` with current-video / `active_video` scope and primary attribute `data-e2e=comment-icon`;
- saving to the temporary config, reloading the page, and waiting for settle restored `评论入口` with the same scope and candidate;
- the per-window inspection table was visible and readable with `窗口 / 元素 / 状态 / 代码 / 诊断` headers;
- strategy `Verified scroll inspection` opened successfully;
- the scroll modal labels were `最少切换视频数` and `最多切换视频数`;
- values `30` and `50` remained after reload;
- screenshots were visually inspected: the element modal/table and scroll modal fit and were readable.

The implementation worker's browser binding was unavailable (`agent.browsers.list() == []`), so it did not substitute standalone Playwright or another browser backend. The parent in-app-browser binding completed the inspection against the same isolated URL before cleanup. No screenshot artifact was saved.

No strategy execution, test-current-draft request, real AdsPower profile start/stop/navigation/click/type/scroll, or user-config mutation occurred.

After inspection, only the temporary port-5018 processes (`57580` and `79152`) were stopped. Port `5018` no longer listened; the pre-existing port-5000 process (`68580`) remained listening and untouched.
The temporary config, its two backups, launcher, and empty launcher logs were then removed from `C:\tmp`; they are not recoverable, and no user data was among them.

## Baseline reconstruction bookkeeping

`tests/test_settings_routes.py` was missing from the original Task 8 snapshot package. For review only, its exact pre-Task-8 state was reconstructed at:

```text
.superpowers/sdd/verified-scroll-task-8-baseline/tests/test_settings_routes.py
```

Method:

1. copy the current file into the snapshot package;
2. remove the Task 8 `_assert_migrated_xpath_elements` helper;
3. revert only the five stale-assertion blocks.

`fc.exe /n` confirms the live/snapshot differences are exactly:

- helper insertion after pre-task line 781;
- canonical replacement around pre-task lines 843–847;
- canonical response/reload/persistence replacement around pre-task lines 926, 929, and 932;
- canonical deletion assertion around pre-task line 963;
- canonical rename assertion around pre-task line 1006;
- schema-v3/lossless migration assertion around pre-task line 1223.

The live file was not changed during reconstruction.

## External boundaries and conclusion

- Supported `tests` root was used; no inaccessible generated directory was deleted.
- No network or live AdsPower action was required.
- Visual inspection, structural live-page checks, and automated UI evidence are complete.
- Persistence, restart, API round-trip, UI-controller refresh, migration, full Python, full Node, and compilation verification are all green.

## Final independent review

Independent verdict:

- Spec Compliance: `PASS`
- Task Quality: `APPROVED`
- Critical findings: `0`
- Important findings: `0`
- Minor findings: `0`
  - the prior implementation-worker browser-availability concern was resolved by the completed isolated parent-side visual inspection.
- Final verdict: `Ready`

## Task 9 Live Acceptance

Date: 2026-07-27  
Status: **BLOCKED**  
Authorized profiles: `…xcto`, `…xctm`  
Production-code changes: none  
Git: not initialized or used

Task 9 completed the authorized live attempt honestly. The read-only locator
draft and persistence checks passed, but the exact-three downward/upward switch
acceptance failed and the comment flow stopped before any click, keyboard input,
or submit. No comment was posted.

### Locator draft

The TikTok template was tested read-only in both open profiles. Each
`active_video` comment entry resolved independently through
`tiktok-comment-entry-primary` (`attribute`). The panel-scoped input and submit
correctly remained unresolved while the comment panel was closed. No click was
dispatched.

### Downward switch result

The temporary canonical strategy used `[3, 3]`, internal delta `+120`, and was
dispatched once without retry:

| Profile | Requested | Completed | Wheel events | Masked transitions | Unmatched/ignored pulses | Code | Recoveries |
|---|---:|---:|---:|---:|---:|---|---:|
| `…xcto` | 3 | 0 | 12 | 0 | 12 | `video_switch_not_observed` | 0 |
| `…xctm` | 3 | 1 | 18 | 1 (`d0→d1`, after 6 pulses) | 17 | `video_switch_not_observed` | 0 |

Ignored pulses were not counted as switches.

Read-only diagnosis found both pages visible and ready, 944×945 viewports,
visible 872×945 `#column-list-container` elements, exactly one stable
center article, no visible dialog/CAPTCHA/login markers, a resolving Task-9
entry locator, a 12-pulse/5-second per-switch budget, and zero lifecycle
recoveries. The supported hypothesis is profile-specific TikTok feed
snap/virtualization responsiveness: the feed did not commit another
center-article identity within the bounded window.

### Upward switch result

`…xcto` never left its initial position and was recorded
`precondition-not-met`; it received no upward dispatch.

`…xctm` still matched the masked `to` identity from its downward transition and
was eligible. Its saved canonical upward action ran exactly once from that
non-initial position through the product runtime, using internal delta `-120`:

| Profile | Dispatches | Requested | Completed | Wheel events | Masked transitions | Unmatched/ignored pulses | Code | Recoveries |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| `…xctm` | 1 | 3 | 1 | 19 | 1 (`u0→u1`, after 7 pulses) | 18 | `video_switch_not_observed` | 0 |

It was not retried.

### Comment flow result

The Task-9 entry locator was reverified on both pages before one independent
comment-flow dispatch per profile. The strategy contained one harmless fixed
acceptance comment whose content was not logged or reported.

- `…xcto` failed its prerequisite switch `0/1` after 12 wheel events with
  `video_switch_not_observed`; no click/input/submit ran.
- `…xctm` reached action 2, so its `1/1` verified-switch prerequisite
  completed, but the entry returned `element_candidate_not_found` before click
  dispatch. The public failure result did not retain that completed switch's
  wheel total.
- Both had zero recoveries and zero click/input/submit dispatches.
- No duplicate dispatch occurred and no comment was posted.

### Refresh, normal-launcher restart, and persistence

The pre-restart fresh dashboard/API signature was:

```text
f086187e12279685a7161e8f63c0c8bf497c5eeb1d083cdbd8b32c3e5f91c247
```

After restart through the normal launcher, the dashboard and API returned HTTP
200 and the signature remained exactly:

```text
f086187e12279685a7161e8f63c0c8bf497c5eeb1d083cdbd8b32c3e5f91c247
```

This preserved Task-9 locator ordering/scopes/candidate types, strategy action
ordering, both `[3, 3]` ranges, and the comment-flow action sequence. The
in-app browser backend was unavailable, so visual screenshot evidence was not
captured; the normal launcher's automatic dashboard open and fresh HTTP/API
structural loads are the available refresh evidence.

The normal launcher/dashboard initialization made its existing read-only
`GET /api/browser/adspower-windows` call. No unrelated profile data was
inspected, retained, or reported, and no unrelated profile was controlled, but
the automatic enumeration is recorded as a strict-boundary concern.

### Cleanup

Only the three Task-9 aliases and two Task-9 strategies were removed through
canonical APIs. Unrelated canonical state matched exact pre-task snapshots:

- elements:
  `32e2f1c0b3c6d2a39eb47beddc056f0f88435369e5ed898341997be3398edb3e`
  before and after;
- strategies:
  `7ea37fa521ec5ddbd26b9636cdff99710fe19a7f87831a1a5b62ca30f92a1475`
  before and after.

Final counts returned to 3 unrelated elements and 1 unrelated strategy. Both
Task-9-opened profiles received one stop request and were confirmed `Inactive`
by a read-only follow-up poll. Temporary recovery snapshots were deleted after
equality verification.

The active `config.json` also matched the canonical API's pre-task copy
byte-for-byte at
`409d9374ef69bedcc7b757cc034e0d3689512671d60ad7e96119c44569d9dba5`.
Canonical PUTs exercised normal five-file backup rotation; managed full-settings
backups were retained rather than manually deleting recovery artifacts. Three
retained point-in-time backups contain temporary Task-9 aliases and one of
those also contains both temporary strategies. The active configuration
contains neither. No retained content is reproduced in this report.

### Post-acceptance log scrub

An independent safety review found exact full profile IDs in canonical execute
responses already retained in `logs/browser_operations.jsonl`. The current log
was scrubbed in place without creating an unredacted backup:

- `…xcto`: 25 exact replacements to ASCII-safe `***xcto`;
- `…xctm`: 17 exact replacements to ASCII-safe `***xctm`;
- expected replacement-only SHA-256 and actual file SHA-256 both equal
  `0d8b81e91c654cf65667f5bc407670f425327b23ca52cddb9de61ab52404c55c`;
- LF/CRLF counts were preserved;
- all 5,088 nonblank lines parsed independently as JSON in original order;
- zero exact full IDs remain in the log or either Task-9 report.

This cleanup does not resolve the production behavior: canonical execute
responses and the browser-operation logger still emit full profile IDs. Task 9
forbids production changes, so response/log masking remains a required
follow-up.

The detailed Task 9 record is in
`.superpowers/sdd/verified-scroll-task-9-report.md`.

## Task 9 production-repair rerun

Date: 2026-07-27  
Status: **BLOCKED before live actions**

The repaired modules passed offline preflight, but the port-5000 production
listener predated those modules. The required normal launcher did not replace
the stale listener before controller interruption, so the rerun correctly sent
no start/tile, locator, strategy, click, keyboard-input, submit, or stop
request. Strategy dispatch counts were zero for both `***xcto` and `***xctm`;
both remained `Inactive`, and no comment was posted.

The collision-free temporary element and three strategies were removed through
canonical settings APIs. Active config, canonical elements, and canonical
strategies returned byte-for-byte to their pre-run signatures. Final JSONL
validation parsed `5,428/5,428` nonblank lines and found neither complete
authorized Profile ID. Post-cleanup compilation exited `0`, and `13/13`
focused masking/route regressions passed.

The controller report is
`.superpowers/sdd/task9-repair-task-6-report.md`.

## Task 9 production-repair resumed live result

Date: 2026-07-27  
Latest status: **FAIL**

After external normal-launcher recovery placed the repaired runtime on port
5000, the approved live sequence resumed from the restored clean baseline. No
additional launcher restart occurred.

Fresh HTTP/API and fresh-process loads reproduced the resumed temporary
definitions, so fresh-load persistence passed. Launcher-restart persistence for
those definitions was **not demonstrated**, because the launcher recovery
preceded their staging.

One start/tile call and one read-only locator draft succeeded for both
`***xcto` and `***xctm`. Each active-video comment entry resolved through the
primary attribute candidate; closed panel fields remained unresolved.

Each profile then received exactly one exact-three downward dispatch:
requested `3`, completed `0`, wheel events `12`, safe transitions `0`,
recoveries `0`, code `video_switch_not_observed`. Neither had the non-initial
precondition, so upward dispatch count was zero.

Each profile also received exactly one one-down→entry-click flow dispatch. Its
verified-down prerequisite requested `1`, completed `0`, and emitted `12`
wheel events before the same failure. The entry action was never reached:
locator attempts `0`, clicks `0`, duplicate clicks `0`, postcondition not
evaluated, keyboard input `0`, submit `0`, comments published `0`.

Cleanup removed only the resumed Task 9 definitions, restored config and
canonical signatures byte-for-byte, sent one stop to each opened profile, and
confirmed both `Inactive` on poll round 3. The four live JSONL records contain
only the approved masked labels. All final `5,435` lines parse and neither
complete authorized Profile ID occurs. Post-cleanup compilation exited `0` and
focused masking/route verification passed `13/13`.

The latest acceptance verdict is **FAIL** because verified switching and the
comment-entry prerequisite did not complete. Safety, privacy, fresh
HTTP/process-load persistence, and cleanup passed; launcher-restart persistence
for the resumed temporary definitions was not demonstrated.
