# TikTok Selector Probe and Self-Healing Design

Date: 2026-07-28  
Status: Approved design  
Scope: TikTok browser-strategy element discovery, validation, publishing, and strategy isolation

Management UI supplement:
[`2026-07-28-selector-probe-management-ui-design.md`](2026-07-28-selector-probe-management-ui-design.md)

## Goal

Extend the existing structured browser-element system with a scheduled,
read-only probe that:

- starts every day at `03:00 Asia/Shanghai`;
- uses only dedicated AdsPower test profiles;
- builds a merged Accessibility Tree and DOM semantic graph;
- validates the current selector bundle before invoking an LLM;
- generates and repairs resilient structured locators;
- proves the same locator bundle in at least two test profiles across two
  consecutive fresh validation rounds;
- publishes only validated selectors to Redis;
- pauses only strategies that depend on failed element aliases;
- retains the last known good selector version without using it to bypass a
  safety pause;
- records alerts in the dashboard and sends sanitized configurable webhooks;
- closes only windows created by the probe.

## Existing foundation

The design extends, rather than replaces, the current implementation:

- `browser_element_schema.py` persists ordered `attribute`, `role`, `css`, and
  `xpath` candidates.
- `browser_element_resolver.py` resolves candidates within explicit scopes and
  checks uniqueness, visibility, enabled state, stability, and obstruction.
- Strategy actions reference element aliases rather than embedding selectors.
- The dashboard already supports locator editing, templates, and read-only
  locator inspection.
- Redis and Celery dependencies already exist, but no selector registry or
  selector-aware strategy gate exists.

The existing locator schema remains the execution contract. Probe-specific
semantic contracts, validation evidence, and version metadata are stored
separately.

## Confirmed product decisions

- Use the incremental hybrid architecture, not a replacement browser agent.
- Retain Playwright Locator as the runtime execution primitive.
- Use deterministic candidate generation before LLM repair.
- Use at least two dedicated AdsPower test profiles.
- Permit only read-only state preparation:
  - navigation;
  - reload;
  - waiting;
  - scrolling;
  - opening and closing a comment panel.
- Prohibit:
  - text input;
  - comment submission;
  - likes;
  - follows;
  - publishing;
  - account-setting changes;
  - arbitrary model-generated actions.
- Pause only strategies that reference failed element aliases.
- Automatically recover probe-paused strategies only after:
  - at least two test profiles pass;
  - two consecutive fresh rounds pass;
  - both rounds validate the same canonical selector bundle;
  - Redis publication succeeds atomically.
- Preserve manual pause independently. Automatic recovery cannot remove a
  manual pause.
- Provide a dashboard alert center and configurable signed webhook.

## Alternatives considered

### Incremental Playwright and CDP extension

Selected.

Advantages:

- reuses the current resolver, scopes, diagnostics, AdsPower lifecycle, and
  strategy aliases;
- keeps the execution path deterministic;
- permits tight side-effect controls;
- introduces the smallest dependency and migration surface.

### Full Stagehand integration

Rejected for the first implementation.

Stagehand provides observation, caching, and self-healing patterns, but a full
integration would add another browser and model runtime and weaken the current
application's explicit scope and action boundaries.

### Vision-first Skyvern-style agent

Rejected.

Vision-first execution is useful for unfamiliar workflows but is too
nondeterministic for unattended comment-related actions. It also adds
substantial runtime, cost, and licensing considerations.

## Open-source references

Implementation may reuse compatible ideas, not copy entire frameworks:

- [microsoft/playwright](https://github.com/microsoft/playwright): retain the
  installed Python runtime, Locator semantics, CDP attachment, and locator
  priority of role, accessible name, label, test ID, then stable attributes.
  Do not add Playwright MCP or CLI as a production dependency.
- [browser-use/browser-use](https://github.com/browser-use/browser-use): adapt
  the ideas of a joined DOM/AX representation, stable fingerprints, and
  cascading semantic matching. Do not adopt its general action agent.
- [browserbase/stagehand](https://github.com/browserbase/stagehand): adapt
  observation-cache invalidation and re-observation/self-heal boundaries. Do
  not add its TypeScript runtime or remote browser service.
- [healenium/healenium-web](https://github.com/healenium/healenium-web): reuse
  only version-history and healing-report concepts. Do not adopt its Selenium
  proxy stack.

AX snapshots can include off-screen nodes. Therefore the design does not treat
AX presence as actionability: DOM layout, visibility, viewport intersection,
scope, uniqueness, and postconditions remain mandatory validation signals.

Before copying code, its exact upstream license and attribution requirements
must be recorded in the repository.

## Architecture

```text
ProbeScheduler
  |
  v
ProbeSessionManager
  |
  v
ProbeStateRunner
  |
  v
SemanticSnapshotExtractor
  |
  v
SelectorCandidateEngine
  |
  v
SelectorRepairEngine
  |
  v
SelectorValidator
  |
  v
SelectorRegistryPublisher
  |
  +--> StrategyGate
  |
  +--> ProbeAlertService
```

### ProbeScheduler

Run the probe in a dedicated long-lived process managed by the normal
application launcher. Do not run it in a Flask background thread or the
existing eventlet Celery worker.

Reasons:

- Flask may have multiple processes;
- Playwright asyncio and eventlet must not share one worker runtime;
- browser cleanup and lease heartbeats need a clear process owner.

Scheduling rules:

- timezone: `Asia/Shanghai`;
- daily time: `03:00`;
- an enabled probe that missed its daily time executes once after service
  recovery;
- it does not replay every missed day;
- Redis lease TTL: 120 seconds;
- lease heartbeat: every 30 seconds;
- a worker that loses the lease stops before publication and enters cleanup;
- a run ID makes repeated delivery idempotent.

### ProbeSessionManager

Input:

- an explicit list of at least two AdsPower test profile IDs;
- target URL and expected safe origin;
- run ID.

Rules:

- reject production or unlisted profile IDs;
- start or attach only through the supported AdsPower/CDP path;
- record whether each browser window was created by this run;
- never close a pre-existing window;
- never enumerate or control unrelated profiles beyond the minimum AdsPower API
  response required to locate configured test profiles;
- mask profile IDs in logs and public responses;
- run cleanup in success, failure, cancellation, and lease-loss paths.

### ProbeStateRunner

Probe state is an explicit state graph, not free-form model execution.

Initial supported states:

```text
feed_ready
comment_panel_open
comment_panel_closed
```

Allowed transitions:

```text
feed_ready -> comment_panel_open
comment_panel_open -> comment_panel_closed
comment_panel_closed -> comment_panel_open
```

Allowed primitives:

- navigate to configured URL;
- reload;
- wait for explicit readiness signals;
- bounded scrolling;
- read-only click on the configured comment-entry intent;
- Escape or read-only close action for the comment panel.

The action dispatcher rejects every non-allowlisted action before it reaches
Playwright. LLM output cannot add transitions or action types.

### SemanticSnapshotExtractor

Accessibility data alone is insufficient because it may omit DOM structure,
ignored nodes, and unique test attributes. DOM alone is insufficient because it
does not directly expose computed accessible name and role.

For each relevant frame and supported scope, collect:

- CDP Accessibility Tree nodes;
- `backendDOMNodeId`;
- DOM tag and stable attributes;
- parent and child relationships;
- computed role and accessible name;
- accessible states and properties;
- bounding box;
- viewport intersection;
- visibility;
- enabled and editable state;
- obstruction and hit-test result;
- supported scope membership.

Join AX and DOM nodes by `backendDOMNodeId`. Output a compact semantic graph
containing only:

- interactive nodes;
- semantic ancestors required to preserve context;
- nearby labels and headings;
- configured stable attributes;
- safe structural relationships.

Do not send or persist full page HTML by default.

### Attribute policy

Preferred attributes:

- `data-e2e`;
- `data-testid`;
- configured site-specific test attributes;
- `aria-label`;
- `aria-labelledby` after resolving its accessible name;
- `name`;
- stable `id`;
- `placeholder`;
- semantic `role`;
- stable `href` fragments when allowed by the element contract.

Reject or penalize:

- full generated class strings;
- React, Vue, or framework instance IDs;
- random or timestamp-shaped values;
- session, user, video, or request IDs;
- absolute DOM positions;
- `nth-child` and long positional chains;
- inline event-handler content;
- text containing user-generated comment or account data.

Every retained attribute must be rechecked for uniqueness inside the configured
scope.

### Element semantic contract

Each managed alias has a probe contract separate from its runtime locators:

```json
{
  "alias": "评论入口",
  "intent": "open the active video's comment panel",
  "required_state": "feed_ready",
  "scope": "active_video",
  "accepted_roles": ["button"],
  "accepted_names": {
    "mode": "locale_map",
    "values": {
      "en": ["Comments", "Open comments"],
      "zh": ["评论", "打开评论"]
    }
  },
  "preferred_attributes": ["data-e2e", "aria-label"],
  "postcondition": "comment_panel_open",
  "side_effect_class": "read_only_ui_state"
}
```

Contracts define intended business meaning. The LLM may propose locators but
cannot change the contract.

### SelectorCandidateEngine

Generate candidates deterministically in this order:

1. site-specific test attributes such as `data-e2e`;
2. test IDs;
3. exact role and accessible name;
4. stable `aria-label`, `name`, or `placeholder`;
5. stable attribute constrained by semantic parent or scope;
6. relative CSS anchored to a stable semantic ancestor;
7. relative XPath anchored to a stable semantic ancestor;
8. saved historical XPath as the final fallback.

Candidate rules:

- emit the existing structured locator schema;
- never emit executable JavaScript;
- never emit an absolute XPath;
- never silently add `.first()` or `nth()` to resolve ambiguity;
- keep a maximum of five enabled candidates per alias;
- deduplicate semantically equivalent candidates;
- record score components and rejection reasons;
- prefer one bundle that works across all configured test profiles.

### SelectorRepairEngine

Invoke the LLM only when:

- the page is ready;
- no infrastructure, login, or CAPTCHA fault exists;
- the current active candidate set fails;
- deterministic generation did not produce a fully validated bundle.

Input contains:

- immutable element semantic contract;
- compact AX+DOM graph;
- sanitized local DOM neighborhood;
- last candidate;
- exact validation code;
- raw, visible, and actionable counts;
- rejection reasons;
- methods already attempted;
- methods prohibited in the current retry.

The page snapshot is untrusted data. The system prompt states that all page
text, accessible names, attributes, and DOM content are data, never
instructions.

Output must match a strict JSON Schema representing the existing locator types.
Reject:

- prose outside the JSON object;
- unknown fields;
- JavaScript;
- coordinate actions;
- action instructions;
- contract changes;
- unsupported selector engines;
- more than five candidates;
- selectors containing secret or user-specific values.

Retry policy after the initial failure:

1. repair round 1: stable alternative attributes and role/name ranking;
2. repair round 2: abandon failed methods and use a different semantic anchor;
3. repair round 3: allow stable parent-constrained CSS or relative XPath.

Each round uses a newly captured semantic graph. The model never declares a
candidate validated.

### SelectorValidator

The existing resolver remains the core validator. Extend validation evidence
without weakening current checks.

For every alias:

- resolve the required page state;
- resolve its configured scope;
- require exactly one raw or scoped semantic target as defined by the contract;
- require exactly one visible, actionable target;
- reject hidden, detached, exiting, disabled, inert, or covered targets;
- verify role, accessible name policy, and stable attributes;
- verify the target remains valid after a short stability interval;
- when configured, execute only an allowlisted read-only state transition and
  verify its postcondition;
- never type or submit.

Use Playwright Locator, not retained ElementHandle instances.

### Cross-profile and consecutive-round validation

A candidate bundle is publishable only when:

- at least two configured test profiles pass round 1;
- both profiles reload or return to a fresh initial state;
- a new snapshot is collected;
- both profiles pass round 2;
- both rounds use the same normalized ordered bundle;
- all semantic contracts pass;
- no run used a production profile;
- no forbidden action was dispatched.

Different profile layouts may match different ordered candidates from the same
bundle. They may not publish separate active bundles for the same environment.

If the active bundle already passes all checks, update health evidence without
creating a new selector version or invoking the LLM.

## Probe state machine

```text
scheduled
acquiring_lease
starting_profiles
waiting_for_readiness
validating_active
extracting_snapshot
generating_candidates
repairing
validating_round_1
resetting_profiles
validating_round_2
persisting_validated_version
publishing
recovering_strategies
cleaning_up
completed
```

Terminal failure states:

- `selector_validation_failed`;
- `probe_unavailable`;
- `probe_lease_lost`;
- `publication_failed`;
- `probe_safety_violation`;
- `probe_cancelled`.

Every terminal path enters cleanup.

## Failure classification

### Selector or semantic failure

Classify as a selector failure only when:

- expected page readiness passed;
- the safe origin is correct;
- the test profile is authenticated;
- no CAPTCHA or blocking challenge is present;
- CDP and Playwright are healthy;
- the required state was reached;
- all three repair rounds failed.

Then:

- retain the previous active and last known good versions;
- do not publish the failed draft;
- pause only strategies referencing failed aliases;
- create or update a deduplicated alert;
- retain sanitized validation evidence and screenshots;
- close probe-owned windows.

The retained last known good version is diagnostic and rollback material. It
does not override the pause.

### Infrastructure, login, CAPTCHA, or CDP failure

Do not modify selectors.

Retry the probe:

- after 15 minutes;
- then after 30 minutes;
- then after 60 minutes.

Set status to `probe_unavailable` and alert. Keep the current published bundle
during this short grace period. If no valid probe completes for 36 consecutive
hours, pause all strategies managed by this TikTok probe. Unrelated strategies
remain available.

### LLM failure

- active bundle passes: remain healthy;
- deterministic candidates pass: publish without LLM;
- active and deterministic candidates fail while the LLM is unavailable:
  pause only affected strategies and alert.

### Redis failure

Redis is required for selector distribution and gates. During Redis failure:

- do not publish;
- do not auto-resume;
- fail closed for strategies managed by the selector registry;
- do not pause unrelated strategies in durable state solely because Redis is
  temporarily unreachable;
- alert and retry projection from the durable outbox.

After Redis recovers, startup reconciliation loads the last published bundle
from SQLite, verifies its stored hash, restores the Redis projection
idempotently, and only then permits managed strategies to run.

## Persistent data model

Use the project's local durable database pattern. SQLite is the authoritative
history and audit store. Exact migrations belong in the implementation plan.

### `selector_versions`

- `id`;
- `site`;
- `environment`;
- `status`: `draft`, `validated`, `published`, `superseded`, `rejected`;
- `base_version_id`;
- `bundle_json`;
- `bundle_hash`;
- `model_id`;
- `prompt_version`;
- `created_at`;
- `validated_at`;
- `published_at`.

### `element_probe_contracts`

- `alias`;
- `site`;
- `environment`;
- `contract_json`;
- `enabled`;
- `updated_at`;
- `updated_by`.

Contracts are validated independently from runtime locators. Removing or
renaming an alias updates contracts, strategy dependencies, and selector drafts
in one locked configuration transaction.

### `selector_validation_runs`

- `id`;
- `selector_version_id`;
- `probe_run_id`;
- `profile_mask`;
- `round_number`;
- `page_state`;
- `result`;
- `failure_code`;
- `evidence_json`;
- `screenshot_path`;
- `started_at`;
- `finished_at`.

### `probe_runs`

- `id`;
- `scheduled_for`;
- `started_at`;
- `finished_at`;
- `status`;
- `active_version_before`;
- `published_version_after`;
- `failed_aliases_json`;
- `details_json`.

### `strategy_gate_reasons`

- `id`;
- `strategy_id`;
- `source`: `probe` or `manual`;
- `reason_code`;
- `aliases_json`;
- `selector_version_id`;
- `created_at`;
- `cleared_at`;
- `cleared_by`.

### `probe_alerts`

- `id`;
- `fingerprint`;
- `status`: `open`, `acknowledged`, `resolved`;
- `failure_class`;
- `aliases_json`;
- `strategy_ids_json`;
- `first_seen_at`;
- `last_seen_at`;
- `occurrence_count`;
- `details_json`;
- `resolved_at`.

### `publication_outbox`

- `id`;
- `event_type`;
- `aggregate_id`;
- `payload_json`;
- `status`;
- `attempt_count`;
- `next_attempt_at`;
- `created_at`;
- `completed_at`.

## Redis model

Namespace all keys by environment:

```text
selector_registry:{env}:tiktok:active
selector_registry:{env}:tiktok:last_known_good
selector_registry:{env}:tiktok:version:{version_id}
selector_registry:{env}:tiktok:probe_status
selector_registry:{env}:tiktok:lease
strategy_gate:{env}:{strategy_id}
```

`active` contains the complete runtime bundle so consumers need one read:

```json
{
  "version": "sel-20260728-030000-...",
  "site": "tiktok",
  "environment": "production",
  "validated_at": "2026-07-28T03:04:12+08:00",
  "profiles_passed": 2,
  "rounds_passed": 2,
  "bundle_hash": "sha256:...",
  "elements": {
    "评论入口": {
      "scope": "active_video",
      "locators": []
    }
  }
}
```

Rules:

- immutable version keys have no application TTL;
- `active`, `last_known_good`, and gate keys have no application TTL;
- only the lease key expires;
- production should use a dedicated Redis instance with AOF persistence,
  capacity monitoring, and `noeviction`;
- a logical Redis database alone does not isolate eviction policy;
- if the first rollout shares the existing Celery Redis instance, `noeviction`
  and capacity limits apply to the whole instance and must be verified before
  enforcement is enabled;
- values contain no credentials, raw DOM, or full profile IDs.

Other interfaces obtain selectors through a server API backed by `active`.
They do not read drafts or model output.

## Atomic publication

SQLite and Redis cannot share one transaction. Use validated durable state, an
outbox, and idempotent Redis publication.

1. In one SQLite transaction:
   - store the complete candidate version as `validated`;
   - store all validation evidence;
   - create a publication-outbox record.
2. Publisher reads the outbox record.
3. A Redis Lua script:
   - verifies the expected previous active version;
   - writes the immutable version key if absent;
   - writes the complete `active` bundle in one operation;
   - updates `last_known_good`;
   - sets probe status to healthy.
4. After Redis confirms success:
   - mark the version `published`;
   - mark the prior version `superseded`;
   - complete the outbox record.
5. Remove only probe-generated gate reasons for aliases covered by the newly
   published version.
6. Leave every manual gate reason unchanged.

Crash handling:

- crash before Redis publication: old active bundle remains;
- crash after Redis publication but before SQLite acknowledgement: startup
  reconciliation compares Redis version and bundle hash with the validated
  outbox record, then completes the durable acknowledgement;
- gate recovery occurs only after publication reconciliation;
- partial selector maps are never visible because `active` is one serialized
  bundle.

## Strategy dependency index and gates

Rebuild the dependency index whenever canonical elements or strategies change:

```text
element alias
  strategy ID
    action ID
    action type
```

Effective strategy status is paused when any uncleared gate reason exists.

Example:

```json
{
  "strategy_id": "comment-flow",
  "effective_status": "paused",
  "reasons": [
    {
      "source": "probe",
      "reason_code": "selector_validation_failed",
      "aliases": ["评论提交按钮"],
      "selector_version_id": "sel-..."
    },
    {
      "source": "manual",
      "reason_code": "operator_pause"
    }
  ]
}
```

Gate checks occur:

1. before scheduling or accepting strategy execution;
2. after a worker receives work;
3. immediately before every action;
4. immediately before dispatching every external side effect.

If a gate appears during execution:

- do not undo or retry an already dispatched action;
- stop before the next action;
- return `strategy_paused_during_execution`;
- preserve completed action evidence;
- do not resume the remainder of the partial run automatically;
- require a new strategy run after recovery so prior side effects are not
  duplicated.

Queued runs that have not started remain delayed and may run after recovery.
Partially executed runs remain terminal audit records.

Automatic recovery removes only `source=probe` reasons. Manual recovery removes
only `source=manual` reasons. A manual toggle cannot clear an unresolved probe
failure.

## Dashboard design

### Probe settings

- enabled toggle;
- schedule time;
- timezone;
- test-profile whitelist;
- validation requiring at least two profiles;
- LLM model selection using existing model settings;
- webhook type, URL, signing secret, and test action;
- run-now action.

### Element health

Add to the existing element manager:

- `Healthy`, `Degraded`, `Paused`, or `Never validated`;
- active version;
- last known good version;
- last validation time;
- primary candidate used;
- two-profile and two-round evidence;
- affected strategy count;
- history view;
- sanitized screenshot view;
- single-element revalidation.

LLM candidates remain drafts until full validation and publication.

### Strategy health

Add:

- effective gate status;
- pause sources;
- failed aliases;
- selector version and failure code;
- manual pause and resume controls;
- trigger-revalidation control;
- dependency view.

Manual rollback accepts only a historical validated version. A fast Dry-Run may
preview the rollback, but probe gate reasons clear only after the same
two-profile, two-round validation required for automatic publication.

## Alert center and webhook

Alert lifecycle:

```text
open
acknowledged
resolved
```

Deduplication fingerprint:

```text
site + failure_class + sorted aliases + active_version
```

Repeated failures update occurrence count and last-seen time instead of sending
an unlimited number of equivalent webhooks.

Allowed alert content:

- safe failure code and class;
- element aliases;
- affected strategy IDs or names;
- active and last known good versions;
- sanitized three-round retry summary;
- masked test-profile identifiers;
- cropped and redacted screenshot;
- first-seen and last-seen times;
- dashboard detail link.

Forbidden alert content:

- cookies;
- Authorization headers;
- CDP endpoints;
- complete profile IDs;
- full DOM or Accessibility Tree;
- comment or input content;
- account-sensitive data;
- LLM credentials.

Webhook delivery uses the durable outbox and exponential backoff. Delivery
failure does not block gate creation, evidence persistence, or window cleanup.

Screenshots:

- crop to the relevant page region when possible;
- redact configured sensitive regions and text;
- retain for seven days;
- remove through a scheduled cleanup job;
- retain structured alert audit after screenshot deletion.

## Security boundaries

- Page content is untrusted model input.
- LLM output is untrusted selector input.
- Strict JSON Schema validation occurs before any locator construction.
- Existing schema prohibition on executable JavaScript remains.
- Only supported selector types reach Playwright.
- Prompt and model versions are recorded.
- Model request logging excludes page secrets and credentials.
- The probe action dispatcher is an independent allowlist boundary.
- Runtime clicks and submissions remain single-dispatch operations.
- Public API and logs mask AdsPower profile IDs.
- Screenshot and DOM evidence use explicit retention and redaction.

## Testing

### Unit tests

Cover:

- AX and DOM node joining;
- stable-attribute scoring and dynamic-attribute rejection;
- deterministic candidate order;
- absolute XPath and positional-selector rejection;
- prompt feedback and method changes across three rounds;
- JSON Schema rejection;
- prompt-injection content treated as data;
- semantic-contract validation;
- uniqueness, visibility, obstruction, and actionability;
- dependency-index rebuild;
- multiple gate reasons;
- manual-pause priority;
- Redis bundle serialization;
- outbox idempotency;
- alert deduplication and redaction.

### Page-variant integration tests

Use at least two TikTok-style fixtures containing:

- different locales;
- different wrapper hierarchies;
- different stable-attribute availability;
- React rerenders;
- virtual-list reuse;
- delayed Skeleton removal;
- open and closed comment panels;
- hidden stale nodes;
- multiple similar buttons;
- off-screen nodes;
- covered and disabled nodes.

One canonical bundle must pass both fixtures in two consecutive fresh rounds.

### Fault injection

Prove:

- Redis failure never creates a partial active bundle;
- SQLite failure never publishes an unrecorded version;
- compare-and-set conflict never overwrites a newer active version;
- crash after Redis success reconciles the outbox;
- LLM timeout and invalid JSON fail safely;
- AdsPower and CDP failure do not rewrite selectors;
- login and CAPTCHA failure are classified as probe unavailable;
- webhook failure does not block cleanup;
- two schedulers produce one lease owner;
- lease loss prevents publication;
- cleanup closes only probe-owned windows;
- automatic recovery preserves manual pause;
- an in-flight dispatched click is never repeated.

### Dedicated-profile live acceptance

Using only configured test profiles:

1. Start two profiles.
2. Load the configured target page.
3. capture AX+DOM snapshots.
4. Validate the comment entry.
5. Open the comment panel through an allowlisted read-only click.
6. Validate input and submit elements without typing or submitting.
7. Reset or reload both profiles.
8. Repeat the complete validation round.
9. Publish the active bundle atomically.
10. Simulate one failed alias and confirm only dependent strategies pause.
11. Restore validation and confirm only probe gate reasons clear.
12. Confirm a manual pause remains.
13. Confirm probe-owned windows close.
14. Confirm unrelated windows remain open.
15. Confirm no comment, like, follow, publish, or account mutation occurred.

## Rollout

### Phase 1: observe-only

- run schedule and test-profile lifecycle;
- extract snapshots;
- validate current selectors;
- generate drafts;
- record alerts;
- do not publish or pause strategies.

Exit criteria:

- seven consecutive successful daily runs;
- no forbidden action;
- no unrelated-window closure;
- alert redaction verified.

### Phase 2: publish without automatic gates

- enable version persistence and Redis publication;
- consumers can read validated active bundles;
- report proposed strategy pauses without enforcing them.

Exit criteria:

- atomic publication and recovery fault tests pass;
- two-profile, two-round live acceptance passes;
- no selector regression in existing strategy execution.

### Phase 3: enforce strategy isolation

- enable probe-generated strategy gates;
- enable strict automatic recovery conditions;
- enable 36-hour probe-unavailable safety pause;
- retain manual pause priority.

## Acceptance criteria

Complete only when:

- scheduling, missed-run behavior, and lease ownership are verified;
- at least two dedicated test profiles pass two consecutive rounds;
- no production profile is controlled;
- no forbidden side effect occurs;
- LLM output cannot bypass deterministic validation;
- Redis exposes only a complete validated active bundle;
- publication is idempotent and crash-recoverable;
- selector failure pauses only dependent strategies;
- Redis failure fails closed only for registry-managed strategies;
- automatic recovery never clears manual pause;
- dashboard health, history, controls, and alerts work;
- webhook delivery is sanitized and retryable;
- abnormal termination cleans up owned windows and leases;
- existing browser-element, strategy, persistence, and UI suites remain green.

## Out of scope

- autonomous discovery of new business actions not defined by semantic
  contracts;
- arbitrary LLM-generated browser actions;
- automatic production-profile probing;
- submitting comments during probe validation;
- solving CAPTCHA;
- replacing Playwright Resolver with a general browser agent;
- supporting every site in the first release;
- cross-origin iframe and closed Shadow DOM targeting unless a confirmed TikTok
  target requires it.
