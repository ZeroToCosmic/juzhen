const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  alertActionModel,
  buildRunPresentation,
  createSelectorProbeUI,
  manualResumeOutcome,
  operationConfirmationIsDangerous,
  renderAlerts,
  renderGates,
  renderOperationWorkspace,
  renderRunStageCards,
  renderRuns,
  renderVersions,
  runTechnicalLines,
  sanitizeAlert,
  sanitizeRun,
  sanitizeVersion,
  selectorProbeDependencies,
  versionActions,
} = require("../gateway/static/selector_probe_ui");

function response(data, status = 200) {
  return {status, data};
}

function node(ownerDocument) {
  return {
    ownerDocument,
    children: [],
    dataset: {},
    attributes: {},
    hidden: false,
    disabled: false,
    textContent: "",
    append(...children) {
      this.children.push(...children);
    },
    replaceChildren(...children) {
      this.children = children;
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
  };
}

function fakeDocument(ids) {
  const nodes = new Map();
  const document = {
    createElement() {
      return node(document);
    },
    querySelector(selector) {
      return selector.startsWith("#") ? nodes.get(selector.slice(1)) || null : null;
    },
  };
  ids.forEach((id) => nodes.set(id, node(document)));
  return {document, nodes};
}

function renderedText(value) {
  return [
    value.textContent,
    ...(value.children || []).flatMap(renderedText),
  ].join(" ");
}

function flattened(value) {
  return [value, ...(value.children || []).flatMap(flattened)];
}

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

test("manual resume copy states when probe reason remains", () => {
  const copy = manualResumeOutcome({
    strategy_id: "strategy-comment",
    reasons: [{source: "probe"}, {source: "manual"}],
  });
  assert.equal(copy.includes("仍将暂停"), true);
  assert.equal(copy.includes("probe"), true);
  assert.equal(copy.includes("strategy-comment"), true);
});

test("alert acknowledgement never advertises strategy recovery", () => {
  const action = alertActionModel({status: "open", gate_active: true});
  assert.equal(action.acknowledge.label, "确认告警");
  assert.equal(action.acknowledge.clears_gate, false);
  assert.equal(action.resolve.disabled, true);
});

test("danger styling covers pause, resolve, retained secret clear, and final confirmation", () => {
  const gateFixture = fakeDocument([
    "selector-gate-counts",
    "selector-gate-rows",
    "selector-gate-status",
  ]);
  renderGates(gateFixture.document, {
    session: {role: "administrator"},
    gates: {
      items: [{
        strategy_id: "strategy-danger",
        effective_status: "active",
        revision: 1,
        reasons: [],
      }],
    },
  });
  const pause = flattened(gateFixture.nodes.get("selector-gate-rows")).find(
    (item) => item.dataset?.gateAction === "pause",
  );
  assert.equal(pause.className, "danger");

  const alertFixture = fakeDocument([
    "selector-alert-counts",
    "selector-alert-rows",
    "selector-alert-status",
  ]);
  renderAlerts(alertFixture.document, {
    session: {role: "administrator"},
    alerts: {items: [{id: 1, status: "open", gate_active: false}]},
  });
  const resolve = flattened(alertFixture.nodes.get("selector-alert-rows")).find(
    (item) => item.dataset?.alertAction === "resolve",
  );
  assert.equal(resolve.className, "danger");
  assert.equal(operationConfirmationIsDangerous({
    kind: "gate-confirm",
    action: "pause",
  }), true);
  assert.equal(operationConfirmationIsDangerous({
    kind: "alert-confirm",
    action: "resolve",
  }), true);
  assert.equal(operationConfirmationIsDangerous({
    kind: "secret-clear-confirm",
  }), true);
  assert.equal(operationConfirmationIsDangerous({
    kind: "settings-confirm",
    dangerousChanges: ["rollout_mode"],
  }), true);
  assert.equal(operationConfirmationIsDangerous({
    kind: "gate-confirm",
    action: "resume",
  }), false);

  const shell = fs.readFileSync(
    path.join(__dirname, "../gateway/templates/_selector_probe_console.html"),
    "utf8",
  );
  assert.equal(
    (shell.match(/class="danger"[^>]+data-settings-secret-clear/g) || []).length,
    2,
  );
});

test("historical version has rollback validation and no activate action", () => {
  const actions = versionActions({status: "superseded"});
  assert.deepEqual(actions.map((item) => item.id), ["rollback-validation"]);
  assert.doesNotMatch(JSON.stringify(actions), /activate|激活此版本/);
});

test("operation sanitizers retain only safe bounded projections", () => {
  const run = sanitizeRun({
    id: "run-1",
    status: "failed",
    profiles: [
      {profile_mask: "***3A7F", status: "passed"},
      {profile_mask: "full-profile-secret", status: "failed"},
    ],
    stages: [{name: "validate", status: "failed", raw_error: "secret"}],
    elements: [{alias: "share", status: "failed", raw_dom: "secret"}],
    raw_dom: "secret",
  });
  const version = sanitizeVersion({
    id: "sel-2",
    status: "conflict",
    diff: {changed_elements: [{alias: "share", change: "updated"}]},
    redis_payload: "secret",
  });
  const alert = sanitizeAlert({
    id: 12,
    status: "open",
    aliases: ["share"],
    screenshot_available: true,
    screenshot_path: "C:/secret/failure.jpg",
    raw_model_output: "secret",
  });

  assert.equal(run.profiles.length, 1);
  assert.equal(run.profiles[0].profile_mask, "***3A7F");
  assert.doesNotMatch(JSON.stringify({run, version, alert}), /secret|raw_dom|screenshot_path/);
  assert.equal(alert.screenshot_url, "/api/selector-probe/alerts/12/screenshot");
});

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
    status: "selector_validation_failed",
    rollout_mode: "observe",
    active_version_before: "sel-stable",
    next_retry_at: "2026-07-31T04:00:00+00:00",
    failed_aliases: ["评论输入框"],
    profiles: ["***3A7F", "***91C2"],
    failure: {
      status: "failed",
      failure_code: "comment_panel_element_missing",
    },
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
  assert.match(
    presentation.failure.nextAction,
    /2026-07-31T04:00:00\+00:00.*重试/,
  );
  assert.deepEqual(
    presentation.run.profiles.map((profile) => profile.profile_mask),
    ["***3A7F", "***91C2"],
  );

  const technical = runTechnicalLines(presentation.run);
  assert.match(technical.join(" "), /comment_panel_transition/);
  assert.match(technical.join(" "), /comment_panel_element_missing/);
  assert.match(technical.join(" "), /60000ms/);
  assert.match(technical.join(" "), /尝试 3/);
});

test("comment transition failure does not mark page loading as failed", () => {
  const presentation = buildRunPresentation({
    id: "run-stage-boundary",
    status: "selector_validation_failed",
    rollout_mode: "observe",
    stages: [
      {name: "page_readiness", status: "passed", profile_mask: "***3A7F"},
      {
        name: "comment_panel_transition",
        status: "failed",
        failure_code: "comment_panel_element_missing",
        profile_mask: "***3A7F",
      },
    ],
  });
  const page = presentation.stages.find((stage) => stage.id === "page");
  const elements = presentation.stages.find(
    (stage) => stage.id === "elements",
  );

  assert.equal(page.status, "success");
  assert.equal(elements.status, "failed");
  assert.equal(presentation.currentStage.id, "elements");
});

test("profile binding collision is reported as environment failure", () => {
  const presentation = buildRunPresentation({
    id: "run-binding-collision",
    status: "infrastructure_unavailable",
    rollout_mode: "enforce",
    stages: [{
      name: "profile_binding",
      status: "failed",
      failure_code: "profile_cdp_collision",
      profile_mask: "***3A7F",
    }],
  });
  const environment = presentation.stages.find(
    (stage) => stage.id === "environment",
  );

  assert.equal(environment.status, "failed");
  assert.equal(presentation.currentStage.id, "environment");
  assert.match(presentation.failure.reason, /Profile|浏览器|连接/);
});

test("real management run terminal failures never present as waiting", () => {
  for (const status of [
    "dispatch_failed",
    "selector_validation_failed",
    "publication_failed",
    "lease_busy",
  ]) {
    const presentation = buildRunPresentation({
      id: `run-${status}`,
      status,
      rollout_mode: "publish",
      ...(status === "dispatch_failed" ? {failure_code: status} : {}),
      failure: {status: "failed", failure_code: status},
      profiles: ["***3A7F", "***91C2"],
      active_version_before: status === "selector_validation_failed"
        ? "sel-stable"
        : "",
    });

    assert.equal(presentation.statusLabel, "失败", status);
    assert.ok(presentation.failure, status);
    assert.equal(presentation.run.failure_code, status);
    assert.notEqual(presentation.currentStage.statusLabel, "等待执行", status);
    if (status === "selector_validation_failed") {
      assert.match(presentation.failure.nextAction, /继续使用上一稳定版/);
    } else {
      assert.match(presentation.failure.nextAction, /等待系统重试或人工确认/);
    }
  }
});

test("failed run without stage evidence never blames environment preparation", () => {
  const presentation = buildRunPresentation({
    id: "request-no-stage",
    status: "dispatch_failed",
    failure: {status: "failed", failure_code: "probe_dispatch_failed"},
    stages: [],
  });

  assert.equal(presentation.statusLabel, "失败");
  assert.equal(presentation.currentStage.id, "unrecorded_failure");
  assert.equal(presentation.currentStage.title, "失败阶段未记录");
  assert.equal(presentation.currentStage.statusLabel, "失败");
  assert.doesNotMatch(presentation.result, /准备测试环境/);
  assert.match(presentation.failure.reason, /调度/);
});

test("observe skips publication and reconciliation before operation failures", () => {
  for (const leaseStatus of ["held", "running", "acquired"]) {
    const presentation = buildRunPresentation({
      id: "run-observe-finalize",
      status: "running",
      rollout_mode: "observe",
      publication: {status: "failed", failure_code: "publication_failed"},
      reconciliation: {status: "running"},
      cleanup: {status: "unknown"},
      lease: {status: leaseStatus},
    });
    const finalStage = presentation.stages[4];

    assert.equal(finalStage.statusLabel, "等待执行", leaseStatus);
    assert.match(finalStage.result, /观察模式，不发布/);
    assert.match(finalStage.result, /本次无需协调/);
    assert.equal(presentation.failure, null);
  }
});

test("completed historical run never presents missing stages as waiting", () => {
  const presentation = buildRunPresentation({
    id: "run-completed-empty",
    status: "completed",
    rollout_mode: "observe",
  });

  assert.equal(presentation.statusLabel, "成功");
  assert.equal(presentation.currentStage.statusLabel, "已跳过");
  assert.doesNotMatch(
    presentation.stages.map((stage) => stage.statusLabel).join(" "),
    /等待执行/,
  );
  assert.ok(presentation.stages.every((stage) => stage.status === "skipped"));
  assert.ok(presentation.stages.every(
    (stage) => stage.result === "历史记录未保留此步骤明细",
  ));
});

test("empty managed catalog asks for collection without infrastructure failure", () => {
  const presentation = buildRunPresentation({
    id: "run-awaiting-elements",
    status: "awaiting_element_selection",
    failure_code: "awaiting_element_selection",
    stages: [
      {name: "prepare_environment", status: "skipped"},
      {name: "open_and_replay", status: "skipped"},
    ],
  });

  assert.equal(presentation.status, "skipped");
  assert.equal(presentation.failure, null);
  assert.match(presentation.result, /采集并保存元素|重新绑定/);
  assert.ok(presentation.stages.every((stage) => stage.status !== "failed"));
});

test("completed partial history skips only missing evidence without inventing success", () => {
  const presentation = buildRunPresentation({
    id: "run-completed-partial",
    status: "completed",
    rollout_mode: "observe",
    stages: [
      {name: "cdp_ready", status: "passed"},
      {name: "page_readiness", status: "passed"},
    ],
    cleanup: {status: "completed"},
    lease: {status: "released"},
  });

  assert.deepEqual(presentation.stages.map((stage) => stage.status), [
    "success", "success", "skipped", "skipped", "success",
  ]);
  assert.equal(
    presentation.stages[2].result,
    "历史记录未保留此步骤明细",
  );
  assert.equal(
    presentation.stages[3].result,
    "历史记录未保留此步骤明细",
  );
  assert.notEqual(presentation.currentStage.status, "waiting");
});

test("technical lines retain sanitized operation failure codes", () => {
  const technical = runTechnicalLines({
    publication: {status: "failed", failure_code: "publication_failed"},
    reconciliation: {status: "failed", failure_code: "reconcile_failed"},
    cleanup: {status: "failed", failure_code: "cleanup_failed"},
    lease: {status: "failed", failure_code: "lease_release_failed"},
  }).join(" ");

  assert.match(technical, /publish.*publication_failed/);
  assert.match(technical, /reconcile.*reconcile_failed/);
  assert.match(technical, /cleanup.*cleanup_failed/);
  assert.match(technical, /lease.*lease_release_failed/);
});

test("gate renderer shows every reason and never offers partial continuation", () => {
  const {document, nodes} = fakeDocument([
    "selector-gate-counts",
    "selector-gate-rows",
    "selector-gate-status",
  ]);
  renderGates(document, {
    session: {role: "administrator"},
    gates: {
      items: [{
        strategy_id: "strategy-comment",
        effective_status: "paused",
        revision: 2,
        reasons: [
          {source: "probe", reason_code: "selector_validation_failed", aliases: ["share"]},
          {source: "manual", reason_code: "maintenance", actor: "admin"},
        ],
      }],
    },
  });

  const text = renderedText(nodes.get("selector-gate-rows"));
  assert.match(text, /probe.*selector_validation_failed/s);
  assert.match(text, /manual.*maintenance/s);
  assert.doesNotMatch(text, /继续部分|continue partial/i);
});

test("manual pause and resume send reason revision and idempotency exactly", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/pause")) {
        return response({
          strategy_id: "strategy-comment",
          revision: 8,
          effective_status: "paused",
          reasons: [{source: "manual", reason_code: "maintenance"}],
        });
      }
      if (url.endsWith("/resume")) {
        return response({
          strategy_id: "strategy-comment",
          revision: 9,
          effective_status: "paused",
          reasons: [{source: "probe", reason_code: "selector_validation_failed"}],
        });
      }
      return response({items: []});
    },
    createIdempotencyKey: (action) => `${action}-key`,
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.confirmManualGate({
    strategy_id: "strategy-comment",
    revision: 7,
    reasons: [],
  }, "pause");
  await ui.submitManualGate("maintenance");
  ui.confirmManualGate({
    strategy_id: "strategy-comment",
    revision: 8,
    reasons: [
      {source: "manual", reason_code: "maintenance"},
      {source: "probe", reason_code: "selector_validation_failed"},
    ],
  }, "resume");
  await ui.submitManualGate("maintenance complete");

  assert.deepEqual(requests.find((item) => item.url.endsWith("/pause")).body, {
    reason: "maintenance",
    expected_revision: 7,
    idempotency_key: "pause-key",
  });
  assert.deepEqual(requests.find((item) => item.url.endsWith("/resume")).body, {
    reason: "maintenance complete",
    expected_revision: 8,
    idempotency_key: "resume-key",
  });
  assert.match(ui.state.operationWorkspace.outcome, /仍将暂停/);
  assert.match(ui.state.operationWorkspace.outcome, /probe/);
});

test("runs render stages and busy run-now opens the active run without continuation", async () => {
  const {document, nodes} = fakeDocument([
    "selector-run-rows",
    "selector-run-status",
  ]);
  renderRuns(document, {
    runs: {
      items: [{
        id: "run-7",
        trigger: "retry",
        status: "infrastructure_failed",
        retry_delay_minutes: 30,
        profiles: [
          {profile_mask: "***3A7F", status: "passed"},
          {profile_mask: "***91C2", status: "failed"},
        ],
        rounds: [
          {profile_mask: "***3A7F", round: 1, status: "passed"},
          {profile_mask: "***3A7F", round: 2, status: "passed"},
          {profile_mask: "***91C2", round: 1, status: "passed"},
          {profile_mask: "***91C2", round: 2, status: "failed"},
        ],
        stages: [{name: "validate", status: "failed", duration_ms: 1200}],
        publication: {status: "not_started"},
        reconciliation: {status: "not_started"},
        cleanup: {status: "completed"},
        lease: {status: "released"},
      }],
    },
  });
  const text = renderedText(nodes.get("selector-run-rows"));
  assert.match(text, /运行 run-7/);
  assert.match(text, /失败/);
  assert.match(text, /当前步骤/);
  assert.match(text, /进度 \d+\/5/);
  assert.match(text, /查看运行/);
  for (const raw of [
    "publish: unknown",
    "reconcile: unknown",
    "cleanup: unknown",
    "lease: unknown",
    "暂无 Profile 证据",
    "暂无轮次证据",
    "暂无阶段证据",
    "暂无元素结果",
    "***3A7F",
    "validate",
  ]) {
    assert.doesNotMatch(text, new RegExp(raw.replaceAll("*", "\\*")));
  }
  assert.doesNotMatch(text, /继续部分|continue partial/i);

  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "operator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/run-now")) {
        return response({error: "probe_busy", active_run_id: "run-active"}, 409);
      }
      if (url.endsWith("/runs/run-active")) {
        return response({id: "run-active", status: "running"});
      }
      return response({items: []});
    },
    createIdempotencyKey: () => "run-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  await ui.requestRunNow();
  assert.deepEqual(requests.find((item) => item.url.endsWith("/run-now")).body, {
    idempotency_key: "run-key",
  });
  assert.equal(ui.state.activeTab, "operations");
  assert.equal(ui.state.operationWorkspace.detail.id, "run-active");
  assert.equal(ui.state.operationWorkspace.error, "probe_busy");
});

test("run detail renders five explained stages and collapsed technical evidence", () => {
  const {document, nodes} = fakeDocument([
    "selector-operation-confirm-dialog",
    "selector-operation-detail-dialog",
    "selector-operation-detail-title",
    "selector-operation-detail-error",
    "selector-operation-detail-body",
    "selector-run-stage-detail",
    "selector-run-technical-details",
    "selector-run-technical-lines",
    "selector-run-discoveries",
    "selector-operation-detail-actions",
  ]);
  const run = {
    id: "run-20",
    status: "selector_validation_failed",
    rollout_mode: "observe",
    failed_aliases: ["评论输入框"],
    profiles: [
      {profile_mask: "***3A7F", status: "passed"},
      {profile_mask: "***91C2", status: "passed"},
    ],
    rounds: [
      {profile_mask: "***3A7F", round: 1, status: "passed"},
      {profile_mask: "***3A7F", round: 2, status: "failed"},
    ],
    stages: [{
      name: "comment_panel_transition",
      status: "failed",
      failure_code: "comment_panel_element_missing",
      profile_mask: "***3A7F",
      round: 2,
      attempt_count: 3,
      duration_ms: 60000,
    }],
    cleanup: {status: "completed"},
    lease: {status: "released"},
  };

  renderOperationWorkspace(document, {
    session: {role: "operator"},
    operationWorkspace: {kind: "run-detail", detail: run},
  });

  const summary = renderedText(nodes.get("selector-operation-detail-body"));
  assert.match(summary, /状态：失败/);
  assert.match(summary, /当前步骤：发现并验证元素/);
  assert.match(summary, /失败原因：/);
  assert.match(summary, /影响范围：/);
  assert.match(summary, /系统下一步：/);
  assert.doesNotMatch(summary, /comment_panel_transition|60000ms|尝试 3/);

  const stageCards = nodes.get("selector-run-stage-detail").children;
  assert.equal(stageCards.length, 5);
  assert.deepEqual(stageCards.map((card) => card.dataset.stageStatus), [
    "success", "waiting", "failed", "failed", "success",
  ]);
  assert.match(renderedText(stageCards[2]), /发现并验证元素/);
  assert.match(renderedText(stageCards[2]), /已保存路径.*Dry-Run/);

  const technicalDetail = nodes.get("selector-run-technical-details");
  assert.equal(technicalDetail.hidden, false);
  assert.equal(technicalDetail.open, false);
  const technical = renderedText(nodes.get("selector-run-technical-lines"));
  assert.match(technical, /comment_panel_transition/);
  assert.match(technical, /comment_panel_element_missing/);
  assert.match(technical, /60000ms/);
  assert.match(technical, /尝试 3/);

  technicalDetail.open = true;
  renderOperationWorkspace(document, {
    session: {role: "operator"},
    operationWorkspace: {kind: "run-detail", detail: run},
  });
  assert.equal(technicalDetail.open, true);
});

test("run stage renderer emits five status cards", () => {
  const {document, nodes} = fakeDocument(["stages"]);
  const presentation = buildRunPresentation({
    status: "completed",
    rollout_mode: "observe",
    profiles: ["***3A7F", "***91C2"],
    rounds: [
      {profile_mask: "***3A7F", round: 1, status: "passed"},
      {profile_mask: "***3A7F", round: 2, status: "passed"},
      {profile_mask: "***91C2", round: 1, status: "passed"},
      {profile_mask: "***91C2", round: 2, status: "passed"},
    ],
    cleanup: {status: "completed"},
    lease: {status: "released"},
  });

  renderRunStageCards(document, nodes.get("stages"), presentation);

  assert.equal(nodes.get("stages").children.length, 5);
  assert.match(renderedText(nodes.get("stages")), /步骤 1.*准备测试环境/s);
  assert.match(renderedText(nodes.get("stages")), /成功/);
});

test("retry always creates a new run request with a safe source ID", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "operator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/run-now")) {
        return response({
          status: "accepted",
          run_id: "run-new",
        }, 202);
      }
      if (url.endsWith("/runs/run-new")) {
        return response({id: "run-new", status: "running"});
      }
      return response({items: [], page: 1, page_size: 20, total: 0});
    },
    createIdempotencyKey: () => "retry-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  await ui.requestRunRetry("run-failed");

  assert.deepEqual(requests.find((item) => item.url.endsWith("/run-now")).body, {
    idempotency_key: "retry-key",
    retry_of_run_id: "run-failed",
  });
  assert.equal(ui.state.operationWorkspace.detail.id, "run-new");
  assert.equal(ui.state.operationWorkspace.detail.status, "running");
});

test("active run detail polls every second and stops at terminal status", async () => {
  const timers = [];
  let detailReads = 0;
  const ui = createSelectorProbeUI({
    requestJson: async (url) => {
      if (url === "/api/auth/session") return response({role: "operator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/runs/run-a")) {
        detailReads += 1;
        return response({
          id: "run-a",
          status: detailReads === 1 ? "running" : "completed",
          stages: [],
        });
      }
      return response({items: [], page: 1, page_size: 20, total: 0});
    },
    setInterval: (callback, milliseconds) => {
      timers.push({callback, milliseconds});
      return timers.length;
    },
    clearInterval() {},
    render() {},
  });
  await ui.init();
  await ui.openRunDetail("run-a");

  assert.equal(ui.state.operationWorkspace.busy, true);
  assert.equal(timers.at(-1).milliseconds, 1000);
  await timers.at(-1).callback();
  assert.equal(ui.state.operationWorkspace.detail.status, "completed");
  assert.equal(ui.state.operationWorkspace.busy, false);
});

test("version renderer distinguishes lifecycle and rollback only creates validation draft", async () => {
  const {document, nodes} = fakeDocument([
    "selector-version-rows",
    "selector-version-status",
  ]);
  renderVersions(document, {
    session: {role: "administrator"},
    versions: {
      items: [
        {id: "sel-active", status: "active"},
        {id: "sel-lkg", status: "lkg"},
        {id: "sel-pending", status: "validated_pending"},
        {id: "sel-old", status: "superseded"},
        {id: "sel-failed", status: "failed"},
        {id: "sel-conflict", status: "conflict"},
      ],
    },
  });
  const text = renderedText(nodes.get("selector-version-rows"));
  for (const label of ["Active", "LKG", "待发布", "已取代", "失败", "冲突"]) {
    assert.match(text, new RegExp(label));
  }
  assert.doesNotMatch(text, /激活此版本/);

  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/rollback-validation")) {
        return response({
          status: "accepted",
          draft_version: "sel-draft-3",
          request_id: "rollback-request",
        }, 202);
      }
      return response({items: []});
    },
    createIdempotencyKey: () => "rollback-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.confirmRollbackValidation({id: "sel-old", status: "superseded"});
  await ui.submitRollbackValidation("restore known layout");
  assert.deepEqual(
    requests.find((item) => item.url.endsWith("/rollback-validation")).body,
    {
      reason: "restore known layout",
      idempotency_key: "rollback-key",
    },
  );
  assert.equal(ui.state.operationWorkspace.draftVersion, "sel-draft-3");
  assert.equal(
    requests.some((item) => /activate/.test(item.url)),
    false,
  );
});

test("alerts show lifecycle and acknowledgement never clears a gate", async () => {
  const {document, nodes} = fakeDocument([
    "selector-alert-counts",
    "selector-alert-rows",
    "selector-alert-status",
  ]);
  renderAlerts(document, {
    session: {role: "operator"},
    alerts: {
      items: [{
        id: 12,
        status: "open",
        severity: "critical",
        failure_class: "selector_validation_failed",
        occurrence_count: 3,
        aliases: ["share"],
        strategy_ids: ["strategy-comment"],
        active_version: "sel-9",
        lkg_version: "sel-8",
        retries: [{delay_minutes: 15, status: "scheduled"}],
        webhook: {status: "pending", attempt_count: 2},
        screenshot_available: true,
        gate_active: true,
      }],
    },
  });
  const text = renderedText(nodes.get("selector-alert-rows"));
  assert.match(text, /open/);
  assert.match(text, /3/);
  assert.match(text, /share/);
  assert.match(text, /strategy-comment/);
  assert.match(text, /sel-9/);
  assert.match(text, /sel-8/);
  assert.match(text, /Webhook.*pending/s);

  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "operator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/acknowledge")) {
        return response({
          id: 12,
          status: "acknowledged",
          gate_active: true,
        });
      }
      return response({items: []});
    },
    createIdempotencyKey: () => "ack-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.confirmAlertAction({id: 12, status: "open", gate_active: true}, "acknowledge");
  await ui.submitAlertAction();
  assert.deepEqual(
    requests.find((item) => item.url.endsWith("/acknowledge")).body,
    {idempotency_key: "ack-key"},
  );
  assert.equal(
    requests.some((item) => /resume|gate/.test(item.url)),
    false,
  );
  assert.equal(
    ui.confirmAlertAction({id: 12, status: "acknowledged", gate_active: true}, "resolve"),
    false,
  );
});

test("manual alert resolve requires admin reason revision and an inactive gate", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/resolve")) {
        return response({id: 18, status: "resolved", gate_active: false});
      }
      return response({items: [], page: 1, page_size: 20, total: 0});
    },
    createIdempotencyKey: () => "resolve-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  assert.equal(ui.confirmAlertAction({
    id: 18,
    status: "acknowledged",
    gate_active: false,
    revision: 4,
  }, "resolve"), true);
  assert.equal(await ui.submitAlertAction("verified recovery"), true);
  assert.deepEqual(requests.find((item) => item.url.endsWith("/resolve")).body, {
    idempotency_key: "resolve-key",
    reason: "verified recovery",
    expected_revision: 4,
  });
  assert.equal(ui.state.operationWorkspace.detail.status, "resolved");
});

test("operations source uses no innerHTML and exposes no partial-run action", () => {
  const source = fs.readFileSync(
    path.join(__dirname, "../gateway/static/selector_probe_ui.js"),
    "utf8",
  );
  assert.doesNotMatch(source, /\.innerHTML\s*=/);
  assert.doesNotMatch(source, /continue[-_ ]partial|继续部分/i);
});
