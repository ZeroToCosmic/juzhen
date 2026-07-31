# Selector Probe Run UI Clarity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace internal probe diagnostics with a clear five-stage run presentation and make both run buttons execute the same operation.

**Architecture:** Build one pure frontend presentation model from the existing sanitized run payload, then use it in both run-list and run-detail rendering. Keep raw stages and operation records in a collapsed technical section; do not change backend endpoints or schemas.

**Tech Stack:** Browser JavaScript, Node.js built-in test runner, Flask-rendered HTML, CSS, pytest

## Global Constraints

- Reuse the current run-list and run-detail API payloads.
- Do not change probe execution, API contracts, Redis, SQLite, scheduling, publication rules, Profile sequencing, or strategy pause/resume behavior.
- Use exactly these user lifecycle states: `等待执行`, `运行中`, `已跳过`, `成功`, `失败`.
- Keep raw stage names, failure codes, attempts, durations, discoveries, and operation records inside collapsed technical details.
- Keep both `selector-probe-run-now` and `selector-run-now`.
- Both run buttons share role visibility, busy state, request behavior, and errors.
- Do not render raw `repairs=0`, ordinary operation `unknown`, or ambiguous missing-evidence labels in run cards.

---

## File Structure

- Modify `gateway/static/selector_probe_ui.js`: shared button wiring, run presentation model, run-list rendering, run-detail rendering.
- Modify `gateway/app.py`: add the technical-details container required by the run-detail renderer.
- Modify `gateway/static/selector_probe.css`: style five-stage cards and collapsed technical details.
- Modify `tests-js/selector-probe-operations.test.js`: button, model, list, detail, and raw-field regression tests.
- Modify `tests/test_selector_probe_console_shell.py`: verify required run-detail containers remain in the Flask shell.

No new runtime file or backend endpoint is required.

### Task 1: Wire and Synchronize Both Run Buttons

**Files:**
- Modify: `gateway/static/selector_probe_ui.js:5360-5364, 5762-5775`
- Test: `tests-js/selector-probe-operations.test.js`

**Interfaces:**
- Consumes: `controller.requestRunNow()`, `state.session.role`, `state.operationWorkspace`, `state.runs.items`, and `runIsActive(run)`.
- Produces: both `#selector-probe-run-now` and `#selector-run-now` dispatch one request per click and share `hidden` and `disabled` properties.

- [ ] **Step 1: Add a two-button browser dependency test**

Import `selectorProbeDependencies` in `tests-js/selector-probe-operations.test.js`:

```javascript
const {
  alertActionModel,
  createSelectorProbeUI,
  manualResumeOutcome,
  operationConfirmationIsDangerous,
  renderAlerts,
  renderGates,
  renderRuns,
  renderVersions,
  sanitizeAlert,
  sanitizeRun,
  sanitizeVersion,
  selectorProbeDependencies,
  versionActions,
} = require("../gateway/static/selector_probe_ui");
```

Add these helpers and test:

```javascript
function eventButton(ownerDocument) {
  const button = node(ownerDocument);
  const listeners = new Map();
  button.addEventListener = (type, callback) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(callback);
  };
  button.emit = (type) => {
    (listeners.get(type) || []).forEach((callback) => callback({target: button}));
  };
  return button;
}

test("both run buttons dispatch once and share visibility and busy state", () => {
  let requests = 0;
  const buttons = [];
  const document = {
    activeElement: null,
    visibilityState: "visible",
    createElement() {
      return node(document);
    },
    getElementById() {
      return null;
    },
    querySelector(selector) {
      if (selector === "#selector-probe-run-now") return buttons[0];
      if (selector === "#selector-run-now") return buttons[1];
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "#selector-probe-run-now, #selector-run-now") {
        return buttons;
      }
      return [];
    },
    addEventListener() {},
    removeEventListener() {},
  };
  buttons.push(eventButton(document), eventButton(document));
  const dependencies = selectorProbeDependencies({
    document,
    navigator: {},
    fetch: async () => ({status: 200, json: async () => ({})}),
    setInterval: () => 1,
    clearInterval() {},
    setTimeout: () => 1,
    clearTimeout() {},
  });
  const controller = {
    requestRunNow() {
      requests += 1;
    },
    state: {
      activeTab: "runs",
      session: {role: "operator"},
      overview: {},
      elements: {items: [], filters: {}},
      gates: {items: []},
      runs: {items: []},
      versions: {items: []},
      alerts: {items: []},
      settings: null,
      operationWorkspace: null,
    },
  };

  dependencies.render("runs", controller.state, controller);
  buttons[0].emit("click");
  assert.equal(requests, 1);
  buttons[1].emit("click");
  assert.equal(requests, 2);
  assert.deepEqual(buttons.map((button) => button.hidden), [false, false]);
  assert.deepEqual(buttons.map((button) => button.disabled), [false, false]);

  controller.state.operationWorkspace = {kind: "run-request", busy: true};
  dependencies.render("runs", controller.state, controller);
  assert.deepEqual(buttons.map((button) => button.disabled), [true, true]);

  controller.state.session = {role: "viewer"};
  dependencies.render("runs", controller.state, controller);
  assert.deepEqual(buttons.map((button) => button.hidden), [true, true]);
});
```

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```powershell
node --test --test-name-pattern="both run buttons" tests-js/selector-probe-operations.test.js
```

Expected: failure because only `#selector-run-now` is wired and synchronized.

- [ ] **Step 3: Use one selector for wiring and state**

Replace the single-button click binding in `wire(controller)` with:

```javascript
document.querySelectorAll(
  "#selector-probe-run-now, #selector-run-now",
).forEach((button) => {
  button.addEventListener("click", () => controller.requestRunNow());
});
```

Replace the single-button render block with:

```javascript
const runNowButtons = document.querySelectorAll(
  "#selector-probe-run-now, #selector-run-now",
);
const runNowHidden = !["administrator", "operator"].includes(
  state.session?.role,
);
const runNowDisabled = (
  (
    state.operationWorkspace?.kind === "run-request"
    && state.operationWorkspace.busy === true
  )
  || (
    state.operationWorkspace?.kind === "run-detail"
    && runIsActive(state.operationWorkspace.detail)
  )
  || (state.runs?.items || []).some(runIsActive)
);
runNowButtons.forEach((button) => {
  button.hidden = runNowHidden;
  button.disabled = runNowDisabled;
});
```

- [ ] **Step 4: Run focused and operations tests**

Run:

```powershell
node --test --test-name-pattern="both run buttons" tests-js/selector-probe-operations.test.js
node --test tests-js/selector-probe-operations.test.js
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- gateway/static/selector_probe_ui.js tests-js/selector-probe-operations.test.js
git commit -m "fix: wire both probe run buttons"
```

### Task 2: Build the Five-Stage Run Presentation Model

**Files:**
- Modify: `gateway/static/selector_probe_ui.js:642-825, 3971-4022, 5820-5860`
- Test: `tests-js/selector-probe-operations.test.js`

**Interfaces:**
- Consumes: `sanitizeRun(raw)`, sanitized stages, profiles, rounds, elements, repairs, publication, reconciliation, cleanup, lease, rollout mode, retry evidence, and failure evidence.
- Produces: `buildRunPresentation(raw) -> {status, statusLabel, currentStage, completedStages, stages, result, failure}` and `runTechnicalLines(raw) -> string[]`.

- [ ] **Step 1: Preserve safe stage scope needed for failure impact**

Update the stage projection in `sanitizeRun`:

```javascript
const stages = Array.isArray(source.stages)
  ? source.stages.slice(0, 30).map((item) => {
    const profileMask = safeText(item?.profile_mask, 16);
    return {
      name: safeCode(item?.name) || "unknown",
      ...safeOperationState(item),
      profile_mask: validProfileMask(profileMask) ? profileMask : "",
      summary: safeText(item?.summary, 240),
    };
  })
  : [];
```

- [ ] **Step 2: Add failing pure-model tests**

Import `buildRunPresentation` and `runTechnicalLines`, then add:

```javascript
test("run presentation explains five stages without raw unknown fields", () => {
  const presentation = buildRunPresentation({
    id: "run-19",
    status: "running",
    rollout_mode: "observe",
    profiles: [
      {profile_mask: "***3A7F", status: "running"},
      {profile_mask: "***91C2", status: "waiting"},
    ],
    rounds: [
      {profile_mask: "***3A7F", round: 1, status: "passed"},
      {profile_mask: "***3A7F", round: 2, status: "running"},
    ],
    stages: [
      {name: "cdp_ready", status: "passed", profile_mask: "***3A7F"},
      {name: "page_readiness", status: "passed", profile_mask: "***3A7F", round: 1},
      {name: "a11y_snapshot", status: "passed", profile_mask: "***3A7F", round: 2},
      {name: "comment_panel_transition", status: "running", profile_mask: "***3A7F", round: 2},
    ],
    publication: {status: "unknown"},
    reconciliation: {status: "unknown"},
    cleanup: {status: "unknown"},
    lease: {status: "running"},
  });

  assert.equal(presentation.statusLabel, "运行中");
  assert.equal(presentation.stages.length, 5);
  assert.deepEqual(presentation.stages.map((stage) => stage.title), [
    "准备测试环境",
    "加载 TikTok 页面",
    "发现并验证元素",
    "两个 Profile 连续两轮确认",
    "发布结果并清理",
  ]);
  assert.equal(presentation.currentStage.title, "发现并验证元素");
  assert.match(presentation.stages[2].result, /评论|元素/);
  assert.match(presentation.stages[3].result, /\*\*\*3A7F.*第 1 轮成功.*第 2 轮运行中/s);
  assert.match(presentation.stages[4].result, /观察模式，不发布/);
  assert.match(presentation.stages[4].result, /本次无需协调/);
  const visible = JSON.stringify({
    statusLabel: presentation.statusLabel,
    stages: presentation.stages,
    result: presentation.result,
    failure: presentation.failure,
  });
  assert.doesNotMatch(visible, /repairs=0|unknown/);
});

test("run presentation distinguishes missing unstable and retry failure evidence", () => {
  const presentation = buildRunPresentation({
    id: "run-failed",
    status: "probe_safety_violation",
    rollout_mode: "observe",
    active_version_before: "sel-stable",
    retry_delay_minutes: 30,
    failed_aliases: ["评论输入框"],
    stages: [{
      name: "comment_panel_transition",
      status: "failed",
      failure_code: "comment_panel_element_missing",
      profile_mask: "***3A7F",
      round: 2,
      attempt_count: 3,
      duration_ms: 60000,
    }],
  });

  assert.equal(presentation.statusLabel, "失败");
  assert.equal(presentation.currentStage.statusLabel, "失败");
  assert.match(presentation.failure.reason, /评论区.*输入框.*提交按钮/);
  assert.match(presentation.failure.impact, /\*\*\*3A7F/);
  assert.match(presentation.failure.impact, /第 2 轮/);
  assert.match(presentation.failure.impact, /评论输入框/);
  assert.match(presentation.failure.nextAction, /30 分钟后重试/);

  const technical = runTechnicalLines(presentation.run);
  assert.match(technical.join(" "), /comment_panel_transition/);
  assert.match(technical.join(" "), /comment_panel_element_missing/);
  assert.match(technical.join(" "), /60000ms/);
  assert.match(technical.join(" "), /尝试 3/);
});
```

- [ ] **Step 3: Run pure-model tests and verify failure**

Run:

```powershell
node --test --test-name-pattern="run presentation" tests-js/selector-probe-operations.test.js
```

Expected: failure because `buildRunPresentation` and `runTechnicalLines` are not exported.

- [ ] **Step 4: Add lifecycle constants and helpers**

Insert after `runIsActive`:

```javascript
const USER_STAGE_STATUS_LABELS = Object.freeze({
  waiting: "等待执行",
  running: "运行中",
  skipped: "已跳过",
  success: "成功",
  failed: "失败",
});
const SUCCESS_OPERATION_STATUSES = new Set([
  "passed", "completed", "success", "succeeded", "published", "released",
]);
const ACTIVE_OPERATION_STATUSES = new Set([
  "running", "processing", "publishing", "reconciling",
]);
const SKIPPED_OPERATION_STATUSES = new Set(["skipped", "not_required"]);
const FAILED_OPERATION_STATUSES = new Set([
  "failed", "error", "conflict", "probe_safety_violation",
  "infrastructure_failed", "infrastructure_unavailable", "probe_unavailable",
]);

function operationLifecycle(raw, options = {}) {
  const operation = safeOperationState(raw);
  if (operation.failure_code || FAILED_OPERATION_STATUSES.has(operation.status)) {
    return "failed";
  }
  if (ACTIVE_OPERATION_STATUSES.has(operation.status)) return "running";
  if (SUCCESS_OPERATION_STATUSES.has(operation.status)) return "success";
  if (SKIPPED_OPERATION_STATUSES.has(operation.status) || options.skip === true) {
    return "skipped";
  }
  return "waiting";
}

function aggregateLifecycle(values, active) {
  const statuses = values.filter(Boolean);
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("running")) return "running";
  if (statuses.length && statuses.every((value) => value === "skipped")) {
    return "skipped";
  }
  if (
    statuses.length
    && statuses.every((value) => ["success", "skipped"].includes(value))
  ) return "success";
  if (active && statuses.some((value) => value !== "waiting")) return "running";
  return "waiting";
}

function leaseAcquisitionLifecycle(raw) {
  const operation = safeOperationState(raw);
  if (operation.failure_code || FAILED_OPERATION_STATUSES.has(operation.status)) {
    return "failed";
  }
  if (["running", "held", "acquired", "released"].includes(operation.status)) {
    return "success";
  }
  return "waiting";
}

function runStatusLifecycle(status) {
  const code = safeCode(status);
  if (ACTIVE_RUN_STATUSES.has(code)) return code === "queued" ? "waiting" : "running";
  if (code === "completed") return "success";
  if (FAILED_OPERATION_STATUSES.has(code)) return "failed";
  return "waiting";
}

function stageSignals(run, names) {
  const allowed = new Set(names);
  return run.stages.filter((stage) => allowed.has(stage.name));
}

function roundStatusLabel(status) {
  return USER_STAGE_STATUS_LABELS[operationLifecycle({status})];
}
```

- [ ] **Step 5: Add the complete presentation builder**

Insert after `sanitizeRun`:

```javascript
const FAILURE_REASON_LABELS = Object.freeze({
  comment_panel_readiness_timeout: "评论区关键控件未在限定时间内就绪",
  comment_panel_element_missing: "评论区缺少输入框或提交按钮",
  comment_panel_snapshot_unstable: "评论区关键控件持续变化，无法确认稳定路径",
  probe_panel_check_failed: "系统无法安全检查评论区",
  element_candidate_not_found: "未找到可验证的元素候选",
  cdp_unavailable: "无法连接测试 Profile 浏览器",
});

function buildRunPresentation(raw) {
  const run = sanitizeRun(raw);
  const active = runIsActive(run);
  const stageStatus = (names) => aggregateLifecycle(
    stageSignals(run, names).map((stage) => operationLifecycle(stage)),
    active,
  );
  const environmentSignals = stageSignals(run, [
    "cdp_endpoint", "cdp_ready", "probe_page_open", "profile_start",
  ]).map((stage) => operationLifecycle(stage));
  if (run.lease.status !== "unknown") {
    environmentSignals.push(leaseAcquisitionLifecycle(run.lease));
  }
  const environmentStatus = aggregateLifecycle(environmentSignals, active);
  const pageStatus = stageStatus(["page_readiness"]);
  const elementSignals = stageSignals(run, [
    "a11y_snapshot", "candidate_filter", "element_dry_run",
    "comment_panel_transition", "comment_panel_cleanup",
    "validate",
  ]).map((stage) => operationLifecycle(stage)).concat(
    run.elements.map((item) => operationLifecycle({
      status: item.status,
      failure_code: item.failure_class,
    })),
  );
  const elementStatus = aggregateLifecycle(elementSignals, active);

  const profileMasks = Array.from(new Set(
    run.profiles.map((profile) => profile.profile_mask)
      .concat(run.rounds.map((round) => round.profile_mask))
      .filter(Boolean),
  ));
  const roundLines = profileMasks.map((profileMask) => {
    const rounds = run.rounds.filter((round) => round.profile_mask === profileMask);
    const line = [1, 2].map((number) => {
      const round = rounds.find((item) => item.round === number);
      return `第 ${number} 轮${round ? roundStatusLabel(round.status) : "等待执行"}`;
    }).join(" / ");
    return `${profileMask}：${line}`;
  });
  const roundStatuses = run.rounds.map((round) => operationLifecycle({
    status: round.status,
    failure_code: round.failure_code,
  }));
  let roundsStatus = aggregateLifecycle(roundStatuses, active);
  const profilesComplete = profileMasks.length >= 2 && profileMasks.every(
    (profileMask) => [1, 2].every((number) => run.rounds.some((round) => (
      round.profile_mask === profileMask
      && round.round === number
      && operationLifecycle({status: round.status}) === "success"
    ))),
  );
  if (profilesComplete) roundsStatus = "success";
  if (!run.rounds.length && !run.profiles.length) roundsStatus = "waiting";

  const observeOnly = run.rollout_mode === "observe";
  const publicationStatus = operationLifecycle(run.publication, {skip: observeOnly});
  const reconciliationStatus = operationLifecycle(
    run.reconciliation,
    {skip: observeOnly},
  );
  const finalStatus = aggregateLifecycle([
    publicationStatus,
    reconciliationStatus,
    operationLifecycle(run.cleanup),
    operationLifecycle(run.lease),
  ], active);

  const stages = [
    {
      id: "environment",
      title: "准备测试环境",
      purpose: "连接两个独立测试 Profile，取得运行锁并打开探针页面。",
      status: environmentStatus,
      result: run.profiles.length
        ? `已记录 ${run.profiles.length}/2 个测试 Profile`
        : "Profile 验证尚未开始",
    },
    {
      id: "page",
      title: "加载 TikTok 页面",
      purpose: "确认页面不是空白、登录、验证码或初始加载状态。",
      status: pageStatus,
      result: pageStatus === "success" ? "TikTok 页面已就绪" : "等待页面完成加载",
    },
    {
      id: "elements",
      title: "发现并验证元素",
      purpose: "读取语义树，寻找关键控件并执行 Dry-Run。",
      status: elementStatus,
      result: run.repairs.length
        ? `已触发自愈 ${run.repairs.length} 次`
        : (run.elements.length
          ? `未触发自愈；已记录 ${run.elements.length} 个元素结果`
          : "未触发自愈；尚未发现可用元素"),
    },
    {
      id: "rounds",
      title: "两个 Profile 连续两轮确认",
      purpose: "排除单账号、单轮或临时网络状态造成的假成功。",
      status: roundsStatus,
      result: roundLines.length
        ? roundLines.join("；")
        : "稳定性验证尚未开始",
    },
    {
      id: "finalize",
      title: "发布结果并清理",
      purpose: "发布验证结果、同步受影响策略、关闭页面并释放运行锁。",
      status: finalStatus,
      result: observeOnly
        ? "观察模式，不发布；本次无需协调"
        : `发布${USER_STAGE_STATUS_LABELS[publicationStatus]}；策略协调${USER_STAGE_STATUS_LABELS[reconciliationStatus]}；清理${USER_STAGE_STATUS_LABELS[operationLifecycle(run.cleanup)]}`,
    },
  ].map((stage) => ({
    ...stage,
    statusLabel: USER_STAGE_STATUS_LABELS[stage.status],
  }));

  const overallStatus = runStatusLifecycle(run.status);
  const failedStage = stages.find((stage) => stage.status === "failed");
  const currentStage = failedStage
    || stages.find((stage) => stage.status === "running")
    || stages.find((stage) => stage.status === "waiting")
    || stages.at(-1);
  const failureStage = run.stages.find(
    (stage) => stage.failure_code || operationLifecycle(stage) === "failed",
  );
  const failureCode = failureStage?.failure_code || run.failure_class;
  const impactParts = [
    failureStage?.profile_mask,
    failureStage?.round ? `第 ${failureStage.round} 轮` : "",
    run.failed_aliases.length ? `元素 ${run.failed_aliases.join("、")}` : "",
  ].filter(Boolean);
  const failure = (failedStage || overallStatus === "failed") ? {
    reason: FAILURE_REASON_LABELS[failureCode] || "探针未完成当前步骤",
    impact: impactParts.join("；") || "等待系统确认",
    nextAction: run.retry_delay_minutes
      ? `系统将在 ${run.retry_delay_minutes} 分钟后重试`
      : (run.next_retry_at
        ? `系统计划在 ${run.next_retry_at} 重试`
        : (run.active_version_before
          ? "继续使用上一稳定版，等待后续验证"
          : "等待系统确认")),
  } : null;

  return {
    run,
    status: overallStatus,
    statusLabel: USER_STAGE_STATUS_LABELS[overallStatus],
    currentStage,
    completedStages: stages.filter(
      (stage) => ["success", "skipped"].includes(stage.status),
    ).length,
    stages,
    result: failure?.reason || currentStage.result,
    failure,
  };
}

function runTechnicalLines(raw) {
  const run = sanitizeRun(raw);
  const stages = run.stages.map((stage) => [
    stage.name,
    stage.status,
    stage.profile_mask ? `Profile ${stage.profile_mask}` : "",
    stage.round ? `第 ${stage.round} 轮` : "",
    stage.attempt_count ? `尝试 ${stage.attempt_count}` : "",
    stage.duration_ms !== null ? `${stage.duration_ms}ms` : "",
    stage.failure_code || "",
  ].filter(Boolean).join(" · "));
  const operations = [
    operationStateText("publish", run.publication),
    operationStateText("reconcile", run.reconciliation),
    operationStateText("cleanup", run.cleanup),
    operationStateText("lease", run.lease),
  ];
  const repairs = run.repairs.map(
    (repair) => `repair ${repair.attempt} · ${repair.failure_code || "unknown"} · ${repair.validation_result || "unknown"}`,
  );
  return stages.concat(operations, repairs);
}
```

Export both new helpers from the module return object:

```javascript
buildRunPresentation,
runTechnicalLines,
```

- [ ] **Step 6: Run focused and sanitizer tests**

Run:

```powershell
node --test --test-name-pattern="run presentation|operation sanitizers" tests-js/selector-probe-operations.test.js
```

Expected: all selected tests pass. No unmasked Profile identifier or raw error field enters the presentation model.

- [ ] **Step 7: Commit Task 2**

```powershell
git add -- gateway/static/selector_probe_ui.js tests-js/selector-probe-operations.test.js
git commit -m "feat: model clear probe run stages"
```

### Task 3: Render Summary, Five Stages, and Technical Details

**Files:**
- Modify: `gateway/static/selector_probe_ui.js:3971-4065, 4696-4710, 4763-4990`
- Modify: `gateway/app.py:2353-2361, 2478-2488`
- Modify: `gateway/static/selector_probe.css:421-460, 593-610`
- Test: `tests-js/selector-probe-operations.test.js`
- Test: `tests/test_selector_probe_console_shell.py`

**Interfaces:**
- Consumes: `buildRunPresentation(raw)` and `runTechnicalLines(raw)` from Task 2.
- Produces: concise run cards, five stage cards, plain-language failure fields, and collapsed raw technical details.

- [ ] **Step 1: Replace the old run renderer test with concise-output assertions**

Update the existing `runs render stages...` test fixture to keep the same run payload, then replace its run-list assertions with:

```javascript
const text = renderedText(nodes.get("selector-run-rows"));
assert.match(text, /运行 run-7/);
assert.match(text, /失败/);
assert.match(text, /当前步骤/);
assert.match(text, /进度 \d+\/5/);
assert.match(text, /查看运行/);
for (const raw of [
  "repairs=0",
  "publish: unknown",
  "reconcile: unknown",
  "cleanup: unknown",
  "lease: unknown",
  "暂无 Profile 证据",
  "暂无轮次证据",
  "暂无阶段证据",
  "暂无元素结果",
]) {
  assert.doesNotMatch(text, new RegExp(raw));
}
```

Add a detail-model renderer test:

```javascript
test("run detail renders five explained stages and collapsed technical evidence", () => {
  const presentation = buildRunPresentation({
    id: "run-20",
    status: "completed",
    rollout_mode: "observe",
    profiles: [
      {profile_mask: "***3A7F", status: "passed"},
      {profile_mask: "***91C2", status: "passed"},
    ],
    rounds: [
      {profile_mask: "***3A7F", round: 1, status: "passed"},
      {profile_mask: "***3A7F", round: 2, status: "passed"},
      {profile_mask: "***91C2", round: 1, status: "passed"},
      {profile_mask: "***91C2", round: 2, status: "passed"},
    ],
    cleanup: {status: "completed"},
    lease: {status: "released"},
  });

  assert.equal(presentation.stages.length, 5);
  assert.equal(presentation.stages[4].statusLabel, "成功");
  assert.match(presentation.stages[4].result, /观察模式，不发布/);
  assert.match(presentation.stages[4].result, /本次无需协调/);
});
```

- [ ] **Step 2: Run rendering tests and verify failure**

Run:

```powershell
node --test --test-name-pattern="runs render|run detail renders" tests-js/selector-probe-operations.test.js
```

Expected: existing run list still dumps raw Profile, round, stage, repair, and operation fields.

- [ ] **Step 3: Replace `runDescription` with summary and technical helpers**

Add:

```javascript
function runSummaryLines(raw) {
  const presentation = buildRunPresentation(raw);
  const lines = [
    `状态：${presentation.statusLabel}`,
    `当前步骤：${presentation.currentStage.title}`,
    `进度：${presentation.completedStages}/5`,
    `结果：${presentation.result}`,
  ];
  if (presentation.failure) {
    lines.push(
      `失败原因：${presentation.failure.reason}`,
      `影响范围：${presentation.failure.impact}`,
      `系统下一步：${presentation.failure.nextAction}`,
    );
  }
  return lines;
}
```

Change the run-detail branch of `operationDetailLines` to:

```javascript
if (workspace?.kind === "run-detail") {
  return runSummaryLines(detail);
}
```

Change `renderRuns` so each card appends only:

```javascript
const presentation = buildRunPresentation(run);
row.append(
  createTextElement(
    document,
    "strong",
    `运行 ${run.id || "未编号"}`,
  ),
  createTextElement(
    document,
    "span",
    `状态：${presentation.statusLabel}`,
    "muted",
  ),
  createTextElement(
    document,
    "span",
    `当前步骤：${presentation.currentStage.title}`,
    "muted",
  ),
  createTextElement(
    document,
    "span",
    `进度 ${presentation.completedStages}/5 · ${presentation.result}`,
    "muted",
  ),
);
```

Keep existing `查看运行` and retry actions.

- [ ] **Step 4: Add run technical-detail containers to the Flask shell**

Replace the run panel description with:

```html
<div><h3>探针运行</h3><p class="muted">显示是否正在运行、当前步骤、步骤目的、两个测试 Profile 的两轮结果，以及失败后的系统动作。</p></div>
```

Replace the run-specific detail containers with:

```html
<section id="selector-run-stage-detail" class="selector-run-stage-list"></section>
<details id="selector-run-technical-details" class="selector-run-technical-details" hidden>
  <summary>技术详情</summary>
  <div id="selector-run-technical-lines" class="selector-operation-list"></div>
  <section id="selector-run-discoveries" class="selector-discovery-list"></section>
</details>
```

- [ ] **Step 5: Render five cards and raw details separately**

Add:

```javascript
function renderRunStageCards(document, container, presentation) {
  if (!container) return;
  container.replaceChildren(...presentation.stages.map((stage, index) => {
    const card = document.createElement("article");
    card.className = "selector-run-stage-card";
    card.dataset.stageStatus = stage.status;
    card.append(
      createTextElement(document, "span", `步骤 ${index + 1}`, "muted"),
      createTextElement(document, "strong", stage.title),
      createTextElement(document, "span", stage.purpose, "muted"),
      createTextElement(
        document,
        "span",
        stage.statusLabel,
        `selector-run-stage-status is-${stage.status}`,
      ),
      createTextElement(document, "span", stage.result),
    );
    return card;
  }));
}
```

In `renderOperationWorkspace`, query the new nodes:

```javascript
const technicalDetail = document.querySelector(
  "#selector-run-technical-details",
);
const technicalLines = document.querySelector(
  "#selector-run-technical-lines",
);
```

Inside the `run-detail` branch, after sanitizing the run:

```javascript
const presentation = buildRunPresentation(run);
renderRunStageCards(document, stageDetail, presentation);
if (technicalDetail) {
  technicalDetail.hidden = false;
  technicalDetail.open = presentation.status === "failed";
}
if (technicalLines) {
  technicalLines.replaceChildren(
    ...runTechnicalLines(run).map((line) => createTextElement(
      document,
      "article",
      line,
      "selector-discovery-row",
    )),
  );
}
```

Keep the existing discovery-row rendering inside
`#selector-run-discoveries`. In the non-run branch clear and hide all
run-specific nodes:

```javascript
stageDetail?.replaceChildren();
discoveryDetail?.replaceChildren();
technicalLines?.replaceChildren();
if (technicalDetail) {
  technicalDetail.hidden = true;
  technicalDetail.open = false;
}
```

Export `renderRunStageCards` for focused tests.

- [ ] **Step 6: Add stage and technical-detail CSS**

Add to `gateway/static/selector_probe.css`:

```css
.selector-run-stage-card {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 7px 12px;
  align-items: center;
  padding: 14px;
  background: rgba(247, 247, 249, 0.72);
  border: 1px solid var(--line, rgba(60, 60, 67, 0.14));
  border-radius: 12px;
}

.selector-run-stage-card > :nth-child(3),
.selector-run-stage-card > :nth-child(5) {
  grid-column: 1 / -1;
}

.selector-run-stage-status {
  justify-self: end;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
}

.selector-run-stage-status.is-running {
  color: #7a4d00;
  background: #fff1cc;
}

.selector-run-stage-status.is-success {
  color: #17653a;
  background: #dcf7e7;
}

.selector-run-stage-status.is-failed {
  color: #9b1c1c;
  background: #fee2e2;
}

.selector-run-stage-status.is-waiting,
.selector-run-stage-status.is-skipped {
  color: #475467;
  background: #eef1f5;
}

.selector-run-technical-details {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--line, rgba(60, 60, 67, 0.14));
  border-radius: 10px;
}

.selector-run-technical-details > summary {
  cursor: pointer;
  font-weight: 700;
}

@media (max-width: 560px) {
  .selector-run-stage-card {
    grid-template-columns: 1fr;
  }

  .selector-run-stage-card > :nth-child(3),
  .selector-run-stage-card > :nth-child(5) {
    grid-column: auto;
  }

  .selector-run-stage-status {
    justify-self: start;
  }
}
```

- [ ] **Step 7: Update Flask shell assertions**

Add these markers to `test_control_page_loads_probe_assets_before_browser_controller`:

```python
'id="selector-run-stage-detail"',
'id="selector-run-technical-details"',
'id="selector-run-technical-lines"',
'id="selector-run-discoveries"',
```

Add CSS assertions:

```python
assert ".selector-run-stage-card" in css
assert ".selector-run-stage-status.is-failed" in css
assert ".selector-run-technical-details" in css
```

- [ ] **Step 8: Run focused, frontend, and Flask regression tests**

Run:

```powershell
node --test tests-js/selector-probe-operations.test.js
npm run test:node
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_console_shell.py tests/test_selector_probe_management_routes.py tests/test_selector_probe_routes.py -q
```

Expected: all tests pass. Run cards contain no raw lifecycle labels, and
technical detail containers remain present and read-only.

- [ ] **Step 9: Review diff and commit Task 3**

Run:

```powershell
git diff --check
git diff -- gateway/static/selector_probe_ui.js gateway/static/selector_probe.css gateway/app.py tests-js/selector-probe-operations.test.js tests/test_selector_probe_console_shell.py
```

Confirm no API, worker, database, Redis, scheduling, Profile, publication, or
strategy-gate code changed. Then commit:

```powershell
git add -- gateway/static/selector_probe_ui.js gateway/static/selector_probe.css gateway/app.py tests-js/selector-probe-operations.test.js tests/test_selector_probe_console_shell.py
git commit -m "feat: explain probe run progress"
```
