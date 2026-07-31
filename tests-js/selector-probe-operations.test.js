const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

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

test("danger styling covers pause, resolve, secret clear, and dangerous final confirmation", () => {
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
    path.join(__dirname, "../gateway/app.py"),
    "utf8",
  );
  assert.equal(
    (shell.match(/class="danger"[^>]+data-settings-secret-clear/g) || []).length,
    3,
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
        repairs: [{attempt: 1, failure_code: "not_found"}],
        publication: {status: "not_started"},
        reconciliation: {status: "not_started"},
        cleanup: {status: "completed"},
        lease: {status: "released"},
      }],
    },
  });
  const text = renderedText(nodes.get("selector-run-rows"));
  assert.match(text, /\*\*\*3A7F/);
  assert.match(text, /\*\*\*91C2/);
  assert.match(text, /第 1 轮/);
  assert.match(text, /第 2 轮/);
  assert.match(text, /30 分钟/);
  assert.match(text, /validate/);
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
  assert.equal(ui.state.activeTab, "runs");
  assert.equal(ui.state.operationWorkspace.detail.id, "run-active");
  assert.equal(ui.state.operationWorkspace.error, "probe_busy");
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
