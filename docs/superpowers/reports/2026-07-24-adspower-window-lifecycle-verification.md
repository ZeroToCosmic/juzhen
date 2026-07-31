# AdsPower Window and Page Lifecycle Verification

Date: 2026-07-24

## Scope and source documents

This verification followed:

- `.superpowers/sdd/adspower-lifecycle-task-7-brief.md`
- `docs/superpowers/specs/2026-07-24-adspower-window-lifecycle-design.md`
- `docs/superpowers/plans/2026-07-24-adspower-window-lifecycle.md`

The report now includes the four review-driven remediation rounds. Production
code and focused regression tests were changed; saved strategies, runtime data,
launcher behavior, AdsPower profiles/windows, and Git state were not changed.

## Third-round remediation

The final review findings were resolved with test-first coverage:

- Browser gateway and strategy-runtime messages now percent-decode and normalize
  bracket, underscore, hyphen, dot, and space key variants before classifying
  sensitive assignments.
- Sensitive values are conservatively projected through comma/semicolon
  continuations until a trusted diagnostic field boundary. URL query/fragment
  and path-shaped credentials are handled structurally.
- Boundary recovery and later same-action recovery events are accumulated in
  chronological order. Nested action events are preserved without a duplicate
  lifecycle event.
- SQLite transaction contexts in `init_db.py` and
  `gateway/account_store.py` now also close their connections explicitly.
  Corresponding test-owned connections were updated in the same narrow scope.

Initial RED evidence:

- Gateway structured-secret table: `6 failed`
- Strategy-runtime explicit and fuzz-like tables: `22 failed`
- Ordered cumulative recovery cases: `3 failed`
- SQLite close probes: `2 failed`
- A later fully percent-encoded marker case and nested recovery integration
  case each produced a focused RED before their fixes.

Mutation evidence:

- Eight temporary mutations were applied one at a time and then reverted.
- The tests killed gateway/runtime percent-decoding removal, gateway/runtime
  comma/semicolon truncation, nested recovery reordering, loss of the later
  wheel failure event, and removal of explicit close behavior from both
  database layers.

## Fourth-round header boundary

The final sanitizer boundary is deliberately narrow:

- Gateway public text and `StrategyRuntimeError.reason` recognize only
  `Cookie` and `Authorization` followed by `:` or `=`.
- Before generic assignment projection, the complete remainder of that line is
  treated as one atomic header value.
- Header-internal `status`, `reason`, `error`, `message`, and `stage`
  assignments cannot become outer safe diagnostic boundaries.
- Only a complete value equal to `missing`, `expired`, `invalid`, or
  `not configured` is preserved after whitespace and matching outer-quote
  normalization. Scheme-prefixed and compound values are redacted.

Test-first evidence:

- Gateway Header matrix RED: `121 failed`, `48 passed`
- Runtime Header matrix RED: `121 failed`, `48 passed`
- Gateway Header matrix GREEN: `169 passed`
- Runtime Header matrix GREEN: `169 passed`
- The matrices cover four Header forms, five internal parameter names,
  comma/semicolon separators, unquoted/single-quoted/double-quoted values, and
  per-case random secrets.

Mutation evidence:

- Removing the gateway or runtime Header projector independently produced
  `121 failed`, `48 passed`.
- Replacing either exact whole-value predicate with the older
  scheme-plus-status predicate made its `Basic missing` regression fail.
- All four mutations were reverted before final verification.

## Automated verification

All pytest executions below were fresh invocations with the cache provider disabled.

### Focused Python suite

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_page_lifecycle.py tests/test_browser_strategy_runtime.py tests/test_browser_actions.py tests/test_window_tiler.py tests/test_browser_routes.py tests/test_app.py -p no:cacheprovider -q -W error
```

The final command also included the database-focused files:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_page_lifecycle.py tests/test_browser_strategy_runtime.py tests/test_browser_actions.py tests/test_window_tiler.py tests/test_browser_routes.py tests/test_app.py tests/test_init_db.py tests/test_account_routes.py -p no:cacheprovider -q -W error
```

Third-round result, repeated as two fresh invocations:

- Run 1: exit `0`, `277 passed`, `0` failed/errors, `108.82s`
- Run 2: exit `0`, `277 passed`, `0` failed/errors, `109.03s`
- No warning filters or warning ignores were used.

Fourth-round final result:

- Exit code: `0`
- Passed: `614`
- Failed/errors: `0`
- Duration: `128.57s` (`0:02:08`)
- No warning filters or warning ignores were used.

### Full Python root discovery

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

Result:

- Exit code: `1`
- Tests executed: `0`
- Collection errors: `1`
- Duration: `3.14s`
- Boundary: pytest root discovery encountered the pre-existing inaccessible directory `work/pytest-tmp`.
- Exact exception: `PermissionError: [WinError 5]` while collecting `work/pytest-tmp`.

The directory was not changed, removed, or traversed further.

### Supported full Python test root

Because root discovery was blocked by the inaccessible generated directory, the supported `tests` root was run directly.

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q
```

Result:

- Exit code: `0`
- Passed: `1227`
- Failed/errors: `0`
- Duration: `345.32s` (`0:05:45`)

### Full Node suite

The brief's literal command was attempted first.

Command:

```powershell
npm test
```

Result:

- Exit code: `1`
- Tests executed: `0`
- Environment boundary: PowerShell blocked the system `npm.ps1` under the current execution policy.

The command was then retried through the equivalent Windows command shim.

Command:

```powershell
npm.cmd test
```

Result:

- Exit code: `1`
- Tests executed: `0`
- Package boundary: `package.json` has no `test` script.

The package's declared full Node test script is `test:node`, which runs `node --test tests-js/*.test.js`.

Command:

```powershell
npm.cmd run test:node
```

Result:

- Exit code: `0`
- Tests: `120`
- Passed: `120`
- Failed: `0`
- Cancelled/skipped/todo: `0/0/0`
- Duration reported by the Node test runner: `570.4354ms`

### Python compilation

Command:

```powershell
.\.venv\Scripts\python.exe -m py_compile browser_page_lifecycle.py browser_actions.py browser_strategy_runtime.py window_tiler.py gateway\app.py init_db.py gateway\account_store.py
```

Result:

- Exit code: `0`
- Compilation errors: `0`

## Read-only environment checks

### Launcher and application

The normal hidden-launcher path is present:

- `start_console.vbs`: present
- `start_console.cmd`: present
- `launcher.py`: present
- `.venv\Scripts\pythonw.exe`: present

The launcher was not started or restarted during verification because the Task 7 brief limits environment checks to read-only operations.

Current application observations:

- `127.0.0.1:5000` had a listening `pythonw` process.
- `GET http://127.0.0.1:5000/ping` returned `{"status":"ok"}`.
- `GET http://127.0.0.1:5000/` returned HTTP `200`.
- The dashboard response size was `110139` bytes.

Therefore the normal launcher artifacts are available and the application was already responding. No claim is made about a fresh launcher start or the absence of transient command windows because no launcher lifecycle action was authorized or performed.

### AdsPower API and profiles

Read-only observations:

- `0.0.0.0:50325` had a listener owned by `AdsPower Global`.
- `GET http://127.0.0.1:50325/status` returned `code=0`, `msg=success`.
- The dashboard's read-only AdsPower profile list returned `21` profiles.
- The application session endpoint listed two session IDs: `***xctm` and `***xcto`.
- A direct read-only AdsPower `browser/active` check returned `Inactive` for both IDs.
- Neither profile's name/group metadata matched an obvious `test`, `disposable`, `sandbox`, `demo`, `测试`, or `临时` label.
- No user message in this task explicitly selected or authorized these two profiles for disposable live testing.

The application's two session records therefore do not establish two active, disposable, explicitly authorized test profiles.

## Live AdsPower acceptance

Status: **Pending — not run**

Reason: two active disposable/test profiles were not explicitly selected and authorized by the user. The two session IDs visible through the application were both reported `Inactive` by AdsPower and were not visibly labeled as test profiles.

The following prohibited live actions were not performed:

- starting or stopping any profile;
- launching any browser;
- moving, resizing, tiling, or foregrounding any real window;
- executing any strategy;
- generating wheel events;
- replacing or closing any Tab.

Read-only primary work-area measurement:

- Work area: `(left=0, top=0, right=1920, bottom=1040)`
- Size: `1920 × 1040`
- Computed two-window target rectangles, not applied:
  - left: `(0, 0, 960, 1040)`
  - right: `(960, 0, 1920, 1040)`

Live evidence unavailable because the test was not authorized:

- Actual window rectangles: not measured
- Foreground result per window: not exercised
- Persistent-topmost behavior: not exercised
- `pause` then `scroll_down`: not executed
- Sampled wheel counts and successful wheel-event counts: not available
- Ordinary-execution window-open state: not exercised
- Sanitized `page_recoveries` replacement event: not produced

No real AdsPower success is claimed.

## Overall status

- Focused lifecycle and database suite: passed (`614/614`)
- Supported full Python test root: passed (`1227/1227`)
- Supported full Node suite: passed (`120/120`)
- Python compilation: passed
- Root pytest discovery: blocked by pre-existing inaccessible `work/pytest-tmp`
- Literal `npm test`: unavailable because PowerShell blocked `npm.ps1`, and the package does not define a `test` script
- Live AdsPower acceptance: pending authorization

Commits: none
