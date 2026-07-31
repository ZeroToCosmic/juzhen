# Selector Probe Minimal Repair Design

Date: 2026-07-30  
Status: Approved design  
Scope: Probe diagnostics, observe discovery, comment-panel state, dynamic element drafts, and strategy dependencies

## Goal

Repair the existing selector probe with the smallest practical change set:

- expose the real lifecycle of one logical probe task;
- diagnose AdsPower Profile and CDP failures without exposing secrets;
- show interactive elements discovered by a successful observe run;
- let an administrator select candidates and save element drafts;
- inspect comment-panel elements through an allowlisted read-only transition;
- populate dynamic element records and strategy dependencies;
- reuse the existing healing, validation, Redis publication, LKG, alert, and
  strategy-gate implementation.

## Confirmed decisions

- Keep the existing Flask, Playwright, AdsPower, SQLite, and Redis architecture.
- Use option A for discovery: show filtered interactive candidates by default
  and allow a sanitized semantic-node expansion.
- Open and close the comment panel automatically when one unique, safe comment
  entry can be resolved.
- Disable repeated manual submission while one probe task is active.
- Selected candidates become drafts. They cannot be used by strategies until
  validation and atomic publication succeed.
- Show masked Profile identity, stage, attempts, duration, and safe error
  summary. Never show full Profile IDs or CDP endpoints.
- Build strategy dependencies automatically when a strategy is saved. No
  manual dependency override.
- Use the minimal repair design instead of a new discovery service or broad
  management-console rebuild.

## Current defects

1. The run list combines `management_run_requests` and `probe_runs`, so five
   accepted requests plus four executions appear as nine runs.
2. Accepted requests never become terminal and are not linked to the actual
   execution.
3. The asynchronous dispatcher discards terminal exceptions.
4. Profile/CDP failures are reduced to broad codes such as
   `profile_open_failed`.
5. Observe evidence exists only in backend validation JSON and is not exposed
   as selectable element candidates.
6. All three legacy comment elements use page scope. The probe therefore does
   not enter the comment-panel state before looking for the input and submit
   controls.
7. The active database has no managed element, element contract, or strategy
   dependency rows even though three legacy action elements remain configured.

## Architecture

```text
manual run or daily 03:00 trigger
  |
  v
one logical management request
  |
  v
linked probe execution and sanitized progress
  |
  v
two dedicated AdsPower Profiles
  |
  +-- feed_ready snapshot and candidates
  |
  +-- safe comment-entry transition
  |
  +-- comment_panel_open snapshot and candidates
  |
  v
administrator selects candidates
  |
  v
managed element drafts and semantic contracts
  |
  v
existing two-profile/two-round validation and publication
  |
  v
existing Redis registry and strategy gates
```

No new service, queue, browser runtime, or model agent is introduced.

## 1. Logical run and diagnostics

### Request lifecycle

Extend `management_run_requests` with:

- nullable `probe_run_id`;
- terminal-capable status:
  `queued`, `running`, `completed`, `failed`, `dispatch_failed`;
- `finished_at`;
- sanitized `failure_code`.

Migrate the existing status constraint rather than maintaining a second status
source. Existing `accepted` rows migrate to `failed` with
`failure_code=legacy_unlinked_request`; existing `dispatch_failed` rows retain
their meaning.

Only one row may have `queued` or `running` status. Enforce this through an
SQLite partial unique index and the existing Redis execution lease.

When an administrator submits while a task is active, return the active task
with `deduplicated=true`. Do not create a second row.

The dispatcher passes the management request ID into `run_tick`. When
`probe_runs` is created, the store links its numeric ID to the request and sets
the request to `running`. Success and every caught terminal failure update the
request to a terminal status. The dispatcher must not silently discard
exceptions.

The management list displays only logical request rows. Scheduled runs also
create a logical request, so manual and scheduled runs share one presentation
model. `probe_runs` remains internal execution and evidence storage.

### Progress representation

Reuse `probe_runs.details_json`. Add a bounded `stages` array and update it
through a store method during execution. Do not create a stage-event table.

Allowed stage names:

- `lease`;
- `ads_status`;
- `profile_start_or_reuse`;
- `cdp_endpoint`;
- `cdp_connectivity`;
- `playwright_connect`;
- `navigate`;
- `page_ready`;
- `snapshot`;
- `candidate_filter`;
- `state_transition`;
- `validation`;
- `cleanup`;
- `lease_release`.

Each projected stage contains:

- masked Profile label when applicable;
- `pending`, `running`, `passed`, or `failed`;
- attempt number from 1 through 3;
- start and finish timestamps;
- duration;
- safe failure code;
- bounded safe summary.

The browser UI polls the current detail endpoint once per second while the task
is active. It does not add SSE or WebSocket infrastructure.

### Profile/CDP behavior

For each configured dedicated Profile:

1. Read current AdsPower state.
2. If inactive, start it once.
3. If active with a valid CDP endpoint, reuse it.
4. If active without an endpoint, poll for endpoint readiness; do not call
   start again.
5. Check endpoint connectivity.
6. Connect Playwright over CDP.
7. Retry a failed start/readiness/connect sequence at most three times.

A Profile started by the current probe may be stopped and restarted after a
failed handshake. A Profile that was already active before the run is never
stopped; an unusable pre-existing session returns
`preexisting_profile_unhealthy`.

Safe error classes distinguish:

- AdsPower API unavailable;
- Profile start rejected;
- active Profile missing endpoint;
- CDP timeout;
- CDP connection refused;
- Playwright CDP connection failure;
- navigation failure;
- readiness blocked by login, CAPTCHA, or skeleton timeout.

Raw AdsPower responses, endpoints, ports, credentials, cookies, and stack
traces never enter public payloads.

## 2. Observe discovery

### Evidence source

Continue storing sanitized semantic snapshots in
`selector_validation_runs.evidence_json`. Do not add a discovery-candidate
table.

The run-detail projector derives candidates from stored semantic nodes and
merges them across masked Profiles using the existing stable semantic
fingerprint. Candidate derivation is deterministic and bounded.

Default candidates include semantic roles that can participate in configured
browser actions, including button, link, textbox, checkbox, radio, combobox,
menu item, tab, and switch. Nodes must be visible and belong to an allowlisted
page scope. Disabled and obstructed nodes remain visible as rejected evidence
but cannot be selected for publication.

Each candidate contains:

- stable fingerprint;
- Role and accessible Name;
- supported state and scope;
- safe stable attributes such as `data-e2e`, `aria-label`, `name`,
  `placeholder`, and stable ID;
- visibility, enabled, editable, viewport, obstruction, and uniqueness result;
- deterministic structured Locator candidates;
- per-Profile presence;
- consistency count.

The UI groups candidates by:

- `feed_ready`;
- `comment_panel_open`.

The default view shows interactive candidates. Expanding a candidate shows its
sanitized semantic node and immediate semantic relationships. The first
version does not implement a full graphical tree editor.

### Candidate selection

An administrator can select one or more candidates and create element drafts.
The draft form pre-fills:

- display name;
- immutable generated alias;
- semantic intent;
- accepted Role;
- accessible Name policy;
- required page state;
- scope;
- preferred stable attributes;
- ordered Locator candidates;
- compatible strategy action type.

A candidate seen in only one Profile may be saved as a draft with a warning.
It cannot publish until the existing two-Profile/two-round gate succeeds with
one canonical bundle.

Observe mode never publishes a candidate, changes Redis, or pauses a strategy.

## 3. Comment-panel state

The minimal state vocabulary remains:

- `feed_ready`;
- `comment_panel_open`;
- `comment_panel_closed` for cleanup verification.

For a successful observe run:

1. Navigate and verify `feed_ready`.
2. Capture the feed semantic snapshot.
3. Resolve the comment entry.
4. Click only when exactly one visible, actionable candidate satisfies the
   comment-entry semantic contract.
5. Verify that the comment panel became visible.
6. Capture the `comment_panel_open` semantic snapshot.
7. Inspect panel candidates without entering text or submitting.
8. Close the panel through an allowlisted close control or Escape.
9. Verify `comment_panel_closed`.

Resolution order:

1. validated published comment-entry Locator;
2. legacy structured Locator Dry-Run;
3. deterministic semantic match from the feed snapshot.

If deterministic matching returns zero or multiple actionable candidates, the
probe does not guess. It completes feed discovery with
`comment_entry_confirmation_required`. The UI lets the administrator select a
feed candidate as the comment-entry draft, then rerun the probe.

No LLM may choose or execute a browser state-transition action.

Legacy element migration assigns:

- comment entry: `feed_ready`, scope `active_video`;
- comment input: `comment_panel_open`, scope `visible_comment_panel`;
- comment submit: `comment_panel_open`, scope `visible_comment_panel`.

The probe may click the comment entry because opening the panel is an approved
read-only transition. It may inspect but never fill the input or click the
submit control.

## 4. Dynamic elements and dependencies

### Legacy migration

Run one idempotent migration from `browser.action_elements`:

1. create one `managed_elements` row per legacy alias;
2. create its `element_probe_contracts` draft;
3. preserve the old structured Locator as historical and LKG evidence;
4. mark management source `legacy_manual`;
5. never delete or rewrite the legacy setting during this repair.

Repeated application startup or migration execution produces no duplicate
rows.

### Strategy dependency sync

After server-side strategy validation and before committing a strategy save:

1. extract element aliases from all action blocks;
2. verify each referenced alias exists and is published;
3. delete dependency rows owned by that strategy;
4. insert the current alias/action relationships;
5. commit the strategy and dependencies in one transaction.

Clients cannot submit or override dependency rows.

Only strategies referencing a failed alias receive the existing automatic
probe gate. Unrelated strategies continue. Automatic recovery removes only
probe-created gate reasons; manual pause reasons remain.

## 5. Existing healing and publication

Do not rewrite the current:

- deterministic selector generation;
- three-attempt LLM feedback repair;
- two dedicated Profile, two consecutive fresh round validator;
- canonical bundle consistency check;
- Redis atomic publication;
- SQLite/Redis reconciliation;
- LKG retention;
- alert center;
- signed Webhook delivery;
- affected-strategy pause and verified recovery.

This repair connects discovered drafts and dependencies to those existing
paths.

Mode boundaries remain:

- `observe`: discover and record only;
- `publish`: validate and publish, without automatic strategy gating;
- `enforce`: publish and enforce affected-strategy gates.

Infrastructure failures do not prove selector failure and therefore do not
pause strategies. They retain LKG and create a probe-availability alert after
the configured retry threshold.

## 6. Management UI

Minimal UI changes:

- run count equals logical tasks only;
- active task disables the run button;
- active detail polls once per second;
- stage list shows Profile, attempt, duration, status, and safe error;
- successful observe detail adds `Discovered elements`;
- candidates group by page state and merge by stable fingerprint;
- selection opens the existing element-draft form;
- dynamic directory displays migrated and newly selected drafts;
- strategy element picker lists published, action-compatible elements only.

No new page framework, canvas tree, browser overlay, or drag-and-drop editor.

## 7. Cleanup and security

- Close every probe-created page in `finally`.
- Stop only a Profile started by the current probe.
- Preserve every pre-existing browser window.
- Release the Redis lease after terminal persistence.
- Store only sanitized semantic evidence.
- Keep full Profile IDs server-side.
- Mask Profile labels in API, UI, logs, alerts, and Webhooks.
- Never persist CDP endpoints, cookies, comment text, credentials, raw prompts,
  raw model output, full DOM, or raw AX trees.

## 8. Acceptance criteria

1. Repeated manual clicks while one task is active return the same task and
   create no extra request.
2. One user-visible task links to one actual `probe_runs` execution.
3. Request status reaches `completed` or `failed`; no new request remains
   permanently `accepted`.
4. Profile/CDP failure shows the exact safe stage, attempt, duration, code, and
   summary.
5. An active Profile without an immediate endpoint is polled and is not
   started repeatedly.
6. Two dedicated test Profiles complete feed snapshot extraction.
7. A unique safe comment entry opens the panel, panel candidates are captured,
   and the panel is closed.
8. Comment input and submit candidates have `comment_panel_open` and
   `visible_comment_panel`.
9. The UI shows merged interactive candidates and expandable sanitized node
   evidence.
10. Selected candidates save as drafts and are unavailable to strategies
    before publication.
11. The three legacy elements migrate exactly once and retain their old
    Locator evidence.
12. Strategy save rebuilds dependency rows atomically from action aliases.
13. Failure of one alias pauses only strategies that reference it in enforce
    mode.
14. Automatic recovery requires two Profiles, two consistent fresh rounds, and
    successful atomic Redis publication.
15. Automatic recovery preserves every manual pause reason.
16. Observe mode performs no Redis publication and no strategy gate mutation.
17. Tests and live evidence contain no full Profile ID, CDP endpoint, cookie,
    credential, comment text, raw DOM, or raw AX tree.
18. One bounded live acceptance run succeeds on both dedicated AdsPower test
    Profiles without touching a production queue.

## Non-goals

- independent discovery microservice;
- new task queue;
- SSE or WebSocket progress;
- generic unlimited page-state graph;
- graphical DOM/AX tree editor;
- injected browser element picker;
- model-generated browser actions;
- replacement of the current validator, registry, alerts, or strategy gates;
- broad refactor of `gateway/app.py`.
