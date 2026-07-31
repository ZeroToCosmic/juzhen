const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  TABS,
  createSelectorProbeUI,
  resolveFocusToken,
  stableFocusToken,
  tabIndexForKey,
  trappedFocusIndex,
  visibleFocusCandidate,
} = require("../gateway/static/selector_probe_ui");

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return {promise, resolve};
}

function harness(overrides = {}) {
  const requests = [];
  const renders = [];
  const intervals = [];
  const dependencies = {
    requestJson: async (url, method = "GET", body, options) => {
      requests.push({url, method, body, options});
      const handler = overrides.responses?.[`${method} ${url}`];
      return typeof handler === "function"
        ? handler({url, method, body, options})
        : handler;
    },
    render: (view, state) => renders.push({
      view,
      activeTab: state.activeTab,
      revision: state[state.activeTab]?.revision,
    }),
    setInterval: (callback, milliseconds) => {
      intervals.push({callback, milliseconds});
      return intervals.length;
    },
    clearInterval: overrides.clearInterval || (() => {}),
    isVisible: overrides.isVisible || (() => true),
    documentVisible: overrides.documentVisible,
    addVisibilityListener: overrides.addVisibilityListener,
    createAbortController: overrides.createAbortController,
    captureFocus: overrides.captureFocus,
    restoreFocus: overrides.restoreFocus,
  };
  return {
    ui: createSelectorProbeUI(dependencies),
    requests,
    renders,
    intervals,
  };
}

test("declares the exact seven management tabs", () => {
  const {ui} = harness();
  assert.deepEqual(
    TABS.map(({id, label}) => [id, label]),
    [
      ["overview", "总览"],
      ["elements", "元素"],
      ["gates", "策略闸门"],
      ["runs", "探针运行"],
      ["versions", "版本"],
      ["alerts", "告警"],
      ["settings", "设置"],
    ],
  );
  assert.deepEqual(ui.tabs, [
    "overview",
    "elements",
    "gates",
    "runs",
    "versions",
    "alerts",
    "settings",
  ]);
});

test("init loads status once and establishes visibility-aware polling", async () => {
  const {ui, requests, intervals} = harness({
    responses: {
      "GET /api/selector-probe/status": {
        status: 200,
        data: {
          registry: {
            available: true,
            active_version: "sel-1",
            bundle_hash: `sha256:${"a".repeat(64)}`,
          },
          latest_run: {
            id: 7,
            status: "completed",
            finished_at: "2026-07-29T03:01:00+08:00",
          },
        },
      },
      "GET /api/auth/session": {
        status: 200,
        data: {role: "administrator"},
      },
    },
  });

  await ui.init();

  assert.deepEqual(
    requests.map(({url, method}) => ({url, method})),
    [
      {url: "/api/auth/session", method: "GET"},
      {url: "/api/selector-probe/status", method: "GET"},
    ],
  );
  assert.equal(ui.state.activeTab, "overview");
  assert.equal(ui.state.overview.registry.active_version, "sel-1");
  assert.equal(ui.state.overview.latest_run.status, "completed");
  assert.equal(intervals.length, 1);
  assert.equal(intervals[0].milliseconds, 15000);
});

test("tab activation refreshes its resource without mutating browser drafts", async () => {
  const browserState = {
    draft: {id: "strategy-1", name: "未保存策略"},
    dirty: true,
    elementDialog: {draft: {alias: "评论入口"}},
  };
  const before = JSON.stringify(browserState);
  const {ui, requests} = harness({
    responses: {
      "GET /api/selector-probe/status": {status: 200, data: {}},
      "GET /api/auth/session": {status: 200, data: {role: "operator"}},
      "GET /api/selector-probe/gates?page=1&page_size=20": {
        status: 200,
        data: {items: [{strategy_id: "strategy-1"}], revision: 2},
      },
    },
  });

  await ui.init();
  await ui.activateTab("gates");

  assert.equal(ui.state.activeTab, "gates");
  assert.equal(ui.state.gates.items.length, 1);
  assert.deepEqual({
    url: requests.at(-1).url,
    method: requests.at(-1).method,
  }, {
    url: "/api/selector-probe/gates?page=1&page_size=20",
    method: "GET",
  });
  assert.equal(JSON.stringify(browserState), before);
});

test("late lower revisions cannot overwrite newer list state", async () => {
  const first = deferred();
  const second = deferred();
  let call = 0;
  const {ui} = harness({
    responses: {
      "GET /api/selector-probe/status": {status: 200, data: {}},
      "GET /api/auth/session": {status: 200, data: {role: "operator"}},
      "GET /api/selector-probe/runs?page=1&page_size=20": () => (
        ++call === 1 ? first.promise : second.promise
      ),
    },
  });

  await ui.init();
  const oldRefresh = ui.activateTab("runs");
  const newRefresh = ui.refreshCurrent();
  second.resolve({
    status: 200,
    data: {items: [{id: "new"}], revision: 9, pagination: {count: 1}},
  });
  await newRefresh;
  first.resolve({
    status: 200,
    data: {items: [{id: "old"}], revision: 8, pagination: {count: 1}},
  });
  await oldRefresh;

  assert.equal(ui.state.runs.revision, 9);
  assert.equal(ui.state.runs.items[0].id, "new");
  assert.equal(ui.acceptRevision("runs", {revision: 8}), false);
  assert.equal(ui.acceptRevision("runs", {revision: 10}), true);
});

test("alerts and settings use Task3 routes while unavailable settings stays explicit", async () => {
  const {ui, requests} = harness({
    responses: {
      "GET /api/selector-probe/status": {status: 200, data: {}},
      "GET /api/auth/session": {status: 200, data: {role: "operator"}},
      "GET /api/selector-probe/alerts?page=1&page_size=20": {
        status: 200,
        data: {items: [], page: 1, page_size: 20, total: 0, revision: 1},
      },
      "GET /api/selector-probe/settings": {
        status: 503,
        data: {error: "settings_unavailable"},
      },
    },
  });

  await ui.init();
  await ui.activateTab("alerts");
  await ui.activateTab("settings");

  assert.equal(requests.length, 4);
  assert.deepEqual(ui.state.elements.items, []);
  assert.deepEqual(ui.state.alerts.items, []);
  assert.equal(ui.state.settings, null);
});

test("destroy clears polling and prevents later refreshes", async () => {
  const cleared = [];
  const {ui, requests, intervals} = harness({
    responses: {
      "GET /api/selector-probe/status": {status: 200, data: {}},
      "GET /api/auth/session": {status: 200, data: {role: "operator"}},
    },
    clearInterval: (timer) => cleared.push(timer),
  });

  await ui.init();
  ui.destroy();
  await intervals[0].callback();

  assert.deepEqual(cleared, [1]);
  assert.equal(requests.length, 2);
  assert.equal(ui.snapshot().destroyed, true);
});

test("polling skips hidden documents, restores immediately, and cleans listener", async () => {
  let visible = true;
  let visibilityHandler = null;
  let listenerRemoved = 0;
  const responses = {
    "GET /api/auth/session": {status: 200, data: {role: "operator"}},
    "GET /api/selector-probe/status": {status: 200, data: {revision: 1}},
    "GET /api/selector-probe/gates?page=1&page_size=20": {
      status: 200,
      data: {items: [], revision: 1},
    },
    "GET /api/selector-probe/alerts?page=1&page_size=20": {
      status: 200,
      data: {items: [], revision: 1},
    },
  };
  const {ui, requests, intervals} = harness({
    responses,
    documentVisible: () => visible,
    addVisibilityListener: (callback) => {
      visibilityHandler = callback;
      return () => {
        listenerRemoved += 1;
      };
    },
  });
  await ui.init();
  visible = false;
  await intervals[0].callback();
  assert.equal(requests.length, 2);

  visible = true;
  await visibilityHandler();
  assert.deepEqual(
    requests.slice(2).map((item) => item.url).sort(),
    [
      "/api/selector-probe/alerts?page=1&page_size=20",
      "/api/selector-probe/gates?page=1&page_size=20",
      "/api/selector-probe/status",
    ].sort(),
  );
  ui.destroy();
  assert.equal(listenerRemoved, 1);
});

test("a new request aborts only the prior request for the same resource", async () => {
  const pending = deferred();
  let statusCalls = 0;
  const controllers = [];
  const {ui} = harness({
    responses: {
      "GET /api/auth/session": {status: 200, data: {role: "operator"}},
      "GET /api/selector-probe/status": () => {
        statusCalls += 1;
        if (statusCalls === 1) return {status: 200, data: {revision: 1}};
        if (statusCalls === 2) return pending.promise;
        return {status: 200, data: {revision: 3}};
      },
    },
    createAbortController: () => {
      const controller = {
        signal: {},
        aborted: false,
        abort() {
          this.aborted = true;
        },
      };
      controllers.push(controller);
      return controller;
    },
  });
  await ui.init();
  const oldRefresh = ui.refreshCurrent();
  const latestRefresh = ui.refreshCurrent();
  await latestRefresh;
  pending.resolve({status: 200, data: {revision: 2}});
  await oldRefresh;
  assert.equal(controllers[1].aborted, true);
  assert.equal(controllers[2].aborted, false);
  assert.equal(ui.state.overview.revision, 3);
});

test("refresh restores its triggering focus after loading and final render", async () => {
  const token = {id: "selector-probe-refresh"};
  const restored = [];
  const {ui} = harness({
    responses: {
      "GET /api/auth/session": {status: 200, data: {role: "operator"}},
      "GET /api/selector-probe/status": {
        status: 200,
        data: {revision: 1},
      },
    },
    captureFocus: () => token,
    restoreFocus: (value) => restored.push(value),
  });
  await ui.init();
  assert.equal(restored.length, 2);
  assert.equal(restored.every((value) => value === token), true);
});

test("seven-tab keyboard navigation wraps and supports activation keys", () => {
  assert.equal(tabIndexForKey(0, "ArrowRight", 7), 1);
  assert.equal(tabIndexForKey(0, "ArrowLeft", 7), 6);
  assert.equal(tabIndexForKey(3, "Home", 7), 0);
  assert.equal(tabIndexForKey(3, "End", 7), 6);
  assert.equal(tabIndexForKey(3, "Enter", 7), 3);
  assert.equal(tabIndexForKey(3, " ", 7), 3);
});

test("dialog focus trap wraps in both directions", () => {
  assert.equal(trappedFocusIndex(2, false, 3), 0);
  assert.equal(trappedFocusIndex(0, true, 3), 2);
  assert.equal(trappedFocusIndex(1, false, 3), 2);
});

test("focus candidates reject hidden ancestors and stable token resolves replacement node", () => {
  const original = {
    id: "refresh-action",
    dataset: {selectorAction: "refresh"},
    name: "",
    tagName: "BUTTON",
  };
  const replacement = {
    id: "refresh-action",
    dataset: {selectorAction: "refresh"},
    tagName: "BUTTON",
  };
  const token = stableFocusToken(original);
  assert.equal(resolveFocusToken({
    getElementById: (id) => (id === "refresh-action" ? replacement : null),
    querySelectorAll: () => [],
  }, token), replacement);
  const dataOnlyToken = stableFocusToken({
    id: "",
    dataset: {selectorAction: "refresh"},
    name: "",
    tagName: "BUTTON",
  });
  assert.equal(resolveFocusToken({
    getElementById: () => null,
    querySelectorAll: (selector) => (
      selector === "[data-selector-action]" ? [replacement] : []
    ),
  }, dataOnlyToken), replacement);
  const actionA = {
    dataset: {selectorAction: "open", targetId: "strategy-a"},
  };
  const actionB = {
    dataset: {selectorAction: "open", targetId: "strategy-b"},
  };
  const collidingToken = stableFocusToken({
    id: "",
    dataset: {selectorAction: "open", targetId: "strategy-b"},
    name: "",
    tagName: "BUTTON",
  });
  assert.equal(resolveFocusToken({
    getElementById: () => null,
    querySelectorAll: (selector) => (
      selector === "[data-selector-action]" ? [actionA, actionB] : []
    ),
  }, collidingToken), actionB);

  assert.equal(visibleFocusCandidate({
    hidden: false,
    disabled: false,
    getAttribute: () => null,
    closest: () => ({hidden: true}),
  }), false);
  assert.equal(visibleFocusCandidate({
    hidden: false,
    disabled: false,
    getAttribute: () => null,
    closest: () => null,
  }, () => ({display: "none", visibility: "visible"})), false);
  assert.equal(visibleFocusCandidate({
    hidden: false,
    disabled: false,
    getAttribute: () => null,
    closest: () => null,
  }, () => ({display: "block", visibility: "visible"})), true);
});

test("console source keeps safe rendering, polite regions, and responsive summaries", () => {
  const uiSource = fs.readFileSync(
    path.join(__dirname, "../gateway/static/selector_probe_ui.js"),
    "utf8",
  );
  const css = fs.readFileSync(
    path.join(__dirname, "../gateway/static/selector_probe.css"),
    "utf8",
  );
  const shellCss = fs.readFileSync(
    path.join(__dirname, "../gateway/static/dashboard_shell.css"),
    "utf8",
  );
  const shell = fs.readFileSync(
    path.join(__dirname, "../gateway/app.py"),
    "utf8",
  );
  assert.doesNotMatch(uiSource, /\.innerHTML\s*=/);
  assert.match(css, /\.selector-summary-grid\s*\{[^}]*repeat\(5,/s);
  assert.match(css, /\.selector-probe-tab\s*\{[^}]*width:\s*auto;/s);
  assert.match(
    css,
    /@media \(max-width: 900px\)[\s\S]*?\.selector-summary-grid\s*\{[^}]*repeat\(2,/,
  );
  assert.match(
    css,
    /@media \(max-width: 560px\)[\s\S]*?\.selector-summary-grid\s*\{[^}]*1fr/,
  );
  assert.match(
    shellCss,
    /@media \(max-width: 900px\)[\s\S]*?\.dashboard-shell\s*\{[^}]*min-width:\s*0;/,
  );
  for (const state of ["", ":hover", ":focus-visible", ":disabled"]) {
    assert.match(css, new RegExp(`button\\.danger${state}`));
  }
  for (const id of [
    "selector-probe-health",
    "selector-probe-unread-alerts",
    "selector-gate-status",
    "selector-version-status",
    "selector-alert-status",
    "selector-account-status",
  ]) {
    const tag = shell.match(new RegExp(`<[^>]+id="${id}"[^>]*>`))?.[0] || "";
    assert.match(tag, /aria-live="polite"/);
  }
});
