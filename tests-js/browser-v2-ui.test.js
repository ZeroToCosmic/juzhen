const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  API_PREFIX,
  actionTemplate,
  apiPath,
  browserV2Dependencies,
  createBrowserV2UI,
  safeEvidencePath,
  stageLabel,
} = require("../gateway/static/browser_v2");

function response(status, data) { return {status, data: data || {}}; }

function harness(overrides = {}) {
  const requests = [];
  const scheduled = [];
  const cleared = [];
  const dependencies = {
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      const item = overrides.responses?.[`${method} ${url}`];
      return typeof item === "function" ? item(body) : (item || response(200, {}));
    },
    setTimeout: (fn, delay) => { const token = {fn, delay}; scheduled.push(token); return token; },
    clearTimeout: (token) => cleared.push(token),
    storage: {getItem: () => null, setItem: () => {}},
  };
  return {ui: createBrowserV2UI(dependencies), requests, scheduled, cleared};
}

function browserWindow(fetch) {
  return {
    fetch,
    setTimeout: (fn, delay) => ({fn, delay}), clearTimeout: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, localStorage: {getItem: () => null, setItem: () => {}},
  };
}

test("URL view selects the requested Browser V2 workspace", () => {
  const window = browserWindow(async () => ({status: 200, json: async () => ({data: []})}));
  window.location = {search: "?view=elements"};

  const ui = createBrowserV2UI(browserV2Dependencies(window));

  assert.equal(ui.state.view, "elements");
  assert.equal(createBrowserV2UI({initialView: "history"}).state.view, "history");
  assert.equal(createBrowserV2UI({initialView: "unknown"}).state.view, "center");
});

function fakeNode(tag = "div") {
  const item = {
    tagName: tag.toUpperCase(), children: [], listeners: {}, value: "", disabled: false,
    append(...children) { this.children.push(...children.filter((child) => child && typeof child === "object")); },
    replaceChildren(...children) { this.children = []; this.append(...children); },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute() {}, removeAttribute() {},
  };
  Object.defineProperty(item, "childElementCount", {get() { return item.children.length; }});
  return item;
}

function descendants(root, tagName) {
  const matches = [];
  function visit(item) {
    if (!item || typeof item !== "object") return;
    if (!tagName || item.tagName === tagName.toUpperCase()) matches.push(item);
    (item.children || []).forEach(visit);
  }
  visit(root);
  return matches;
}

function pickerDocument() {
  const fields = {
    "#v2-profile-list": fakeNode(),
    "#v2-picker-profile": fakeNode("select"),
    "#v2-picker-url": fakeNode("input"),
    "#v2-picker-start": fakeNode("button"),
    "#v2-picker-finish": fakeNode("button"),
    "#v2-picker-cancel": fakeNode("button"),
    "#v2-picker-state": fakeNode(),
    "#v2-picker-candidates": fakeNode(),
  };
  return {
    fields,
    createElement: (tag) => fakeNode(tag),
    querySelector: (selector) => fields[selector] || null,
    querySelectorAll: () => [],
  };
}

function strategyDocument() {
  const fields = {
    "#v2-strategy-list": fakeNode(),
    "#v2-strategy-editor": fakeNode(),
    "#v2-strategy-empty": fakeNode(),
    "#v2-action-list": fakeNode("ol"),
    "#v2-strategy-name": fakeNode("input"),
    "#v2-strategy-target-url": fakeNode("input"),
    "#v2-strategy-ready-element": fakeNode("select"),
    "#v2-strategy-readiness-timeout": fakeNode("input"),
    "#v2-strategy-run-mode": fakeNode("select"),
    "#v2-strategy-minutes": fakeNode("input"),
    "#v2-strategy-enabled": fakeNode("input"),
    "#v2-strategy-minutes-wrap": fakeNode(),
  };
  fields["#v2-strategy-enabled"].checked = true;
  return {
    fields,
    createElement: (tag) => fakeNode(tag),
    querySelector: (selector) => fields[selector] || null,
    querySelectorAll: () => [],
  };
}

test("V2 API path is exclusive", () => {
  assert.equal(apiPath("/api/browser-v2/jobs"), "/api/browser-v2/jobs");
  assert.throws(() => apiPath("/api/browser/jobs"), /只能调用 V2/);
  assert.equal(API_PREFIX, "/api/browser-v2");
});

test("window tiling stage has an explicit Chinese label", () => {
  assert.equal(stageLabel("window_tile"), "正在排列窗口");
});

test("scroll editor defines verified video switches without pixel controls", () => {
  assert.deepEqual(actionTemplate("scroll", "scroll-1"), {
    id: "scroll-1", type: "scroll", direction: "down",
    distance_pixels: [120, 120], count: [1, 2], interval_seconds: [0.2, 0.5],
  });
  const source = fs.readFileSync(path.join(__dirname, "..", "gateway", "static", "browser_v2.js"), "utf8");
  assert.equal(source.includes("滚动像素范围"), false);
  assert.equal(source.includes("视频切换次数范围"), true);
  assert.equal(source.includes("每次发送一个方向键，并在确认视频切换后继续。"), true);
  assert.equal(source.includes('scroll: {label: "切换视频"}'), true);
  assert.match(source, /视频切换.*completed_switches/);
  assert.equal(source.includes("滚轮事件"), false);
  assert.equal(source.includes("wheelCalibration"), false);
  assert.equal(source.includes("/wheel-calibration"), false);
});

test("template has exactly five V2 views and excludes retired vocabulary", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "gateway", "templates", "browser_v2.html"), "utf8");
  const sidebar = fs.readFileSync(path.join(__dirname, "..", "gateway", "templates", "_dashboard_sidebar.html"), "utf8");
  assert.deepEqual([...html.matchAll(/data-panel="([^"]+)"/g)].map((match) => match[1]), ["center", "elements", "strategies", "history", "settings"]);
  ["probe", "repairs", "publish", "reconcile", "lease", "semantic", "unknown"].forEach((word) => assert.equal(html.toLowerCase().includes(word), false));
  assert.equal(html.includes("滚轮校准"), false);
  assert.match(html, /data-action-type="scroll">切换视频/);
  assert.match(html, /dashboard_shell\.css/);
  assert.match(html, /class="dashboard-shell"/);
  assert.match(html, /_dashboard_sidebar\.html/);
  assert.match(html, /class="dashboard-main"/);
  assert.doesNotMatch(sidebar, /href="\/\?panel=strategies"/);
  const v2Link = sidebar.match(/<a[^>]+href="\/console\/actions"[^>]*>/)[0];
  assert.match(v2Link, /class="dashboard-nav-link/);
  assert.match(v2Link, /action-library/);
});

test("five action blocks compose, reorder, and delete without a DOM", () => {
  const {ui} = harness();
  ui.state.draft = {id: "s1", name: "策略", definition: {actions: []}};
  ["move", "scroll", "click", "input", "wait"].forEach((type) => assert.equal(ui.addAction(type), true));
  assert.deepEqual(ui.state.draft.definition.actions.map((item) => item.type), ["move", "scroll", "click", "input", "wait"]);
  assert.equal(ui.moveAction(4, -1), true);
  assert.deepEqual(ui.state.draft.definition.actions.map((item) => item.type), ["move", "scroll", "click", "wait", "input"]);
  assert.equal(ui.removeAction(1), true);
  assert.deepEqual(ui.state.draft.definition.actions.map((item) => item.type), ["move", "click", "wait", "input"]);
  assert.deepEqual(actionTemplate("wait", "a"), {id: "a", type: "wait", duration_seconds: [1, 1]});
});

test("adding actions to an existing strategy skips IDs already in use", () => {
  const {ui} = harness();
  ui.state.draft = {
    id: "strategy-1",
    name: "Existing strategy",
    definition: {
      actions: [actionTemplate("wait", "action_1"), actionTemplate("wait", "action_2")],
    },
  };

  assert.equal(ui.addAction("scroll"), true);
  assert.equal(ui.addAction("wait"), true);

  const ids = ui.state.draft.definition.actions.map((item) => item.id);
  assert.deepEqual(ids, ["action_1", "action_2", "action_3", "action_4"]);
  assert.equal(new Set(ids).size, ids.length);
});

test("no active job or picker means no polling; active work polls once per second and stops terminal", async () => {
  const {ui, scheduled, cleared} = harness({
    responses: {"GET /api/browser-v2/jobs/job-1": response(200, {data: {id: "job-1", status: "completed"}})},
  });
  ui.syncPolling();
  assert.equal(scheduled.length, 0);
  ui.state.job = {id: "job-1", status: "running"};
  ui.syncPolling();
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].delay, 1000);
  await scheduled[0].fn();
  assert.equal(scheduled.length, 1);
  ui.stopPolling();
  assert.equal(cleared.length, 0);
});

test("strategy static fields synchronize into draft", () => {
  const document = strategyDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-new", localNew: true, name: "旧名称", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: []},
  };
  const fields = document.fields;
  fields["#v2-strategy-name"].value = "新名称"; fields["#v2-strategy-name"].listeners.input();
  fields["#v2-strategy-target-url"].value = "https://www.tiktok.com/foryou"; fields["#v2-strategy-target-url"].listeners.input();
  fields["#v2-strategy-ready-element"].value = "ready-1"; fields["#v2-strategy-ready-element"].listeners.change();
  fields["#v2-strategy-readiness-timeout"].value = "30"; fields["#v2-strategy-readiness-timeout"].listeners.input();
  fields["#v2-strategy-minutes"].value = "2-5"; fields["#v2-strategy-minutes"].listeners.input();
  fields["#v2-strategy-enabled"].checked = false; fields["#v2-strategy-enabled"].listeners.change();
  fields["#v2-strategy-run-mode"].value = "duration"; fields["#v2-strategy-run-mode"].listeners.change();

  assert.equal(ui.state.draft.name, "新名称");
  assert.equal(ui.state.draft.definition.target_url, "https://www.tiktok.com/foryou");
  assert.equal(ui.state.draft.definition.ready_element_id, "ready-1");
  assert.equal(ui.state.draft.definition.readiness_timeout_seconds, "30");
  assert.equal(ui.state.draft.definition.loop_duration_minutes, "2-5");
  assert.equal(ui.state.draft.definition.run_mode, "duration");
  assert.equal(ui.state.draft.enabled, false);
  assert.equal(fields["#v2-strategy-minutes-wrap"].hidden, false);
});

test("flat API strategy opens as a nested editor draft", () => {
  const document = strategyDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.strategies = [{
    id: "strategy-1", name: "评论策略", enabled: true, revision: 1,
    target_url: "https://www.tiktok.com/", ready_element_id: "ready-1",
    readiness_timeout_seconds: 30, run_mode: "once", loop_duration_minutes: null,
    actions: [actionTemplate("wait", "wait-1")],
  }];

  ui.render();
  document.fields["#v2-strategy-list"].children[0].children[1].listeners.click();

  assert.equal(ui.state.draft.definition.target_url, "https://www.tiktok.com/");
  assert.deepEqual(ui.state.draft.definition.actions, [actionTemplate("wait", "wait-1")]);
  assert.equal(Object.hasOwn(ui.state.draft, "actions"), false);
});

test("successful strategy save keeps a nested editable draft", async () => {
  const document = strategyDocument();
  const fields = document.fields;
  fields["#v2-strategy-name"].value = "评论策略";
  fields["#v2-strategy-target-url"].value = "https://www.tiktok.com/";
  fields["#v2-strategy-ready-element"].value = "ready-1";
  fields["#v2-strategy-readiness-timeout"].value = "30";
  fields["#v2-strategy-run-mode"].value = "once";
  const flat = {
    id: "strategy-1", name: "评论策略", enabled: true, revision: 1,
    target_url: "https://www.tiktok.com/", ready_element_id: "ready-1",
    readiness_timeout_seconds: 30, run_mode: "once", loop_duration_minutes: null,
    actions: [actionTemplate("wait", "wait-1")],
  };
  const ui = createBrowserV2UI({
    document,
    requestJson: async (_url, method) => method === "POST"
      ? response(201, {data: flat}) : response(200, {data: [flat]}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-1", localNew: true, name: "评论策略", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "ready-1", readiness_timeout_seconds: 30, run_mode: "once", loop_duration_minutes: null, actions: [actionTemplate("wait", "wait-1")]},
  };

  assert.equal(await ui.saveStrategy(), true);
  assert.deepEqual(ui.state.draft.definition.actions, [actionTemplate("wait", "wait-1")]);
  assert.equal(ui.state.draft.localNew, false);
});

test("active picker polling does not rebuild strategy action editors", async () => {
  const document = strategyDocument();
  const scheduled = [];
  const ui = createBrowserV2UI({
    document,
    requestJson: async () => response(200, {data: {id: "picker-1", status: "waiting_for_selection"}}),
    setTimeout: (fn, delay) => { const timer = {fn, delay}; scheduled.push(timer); return timer; },
    clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-new", localNew: true, name: "策略", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: [actionTemplate("wait", "wait-1")]},
  };
  ui.state.picker = {id: "picker-1", status: "waiting_for_selection"};
  ui.render();
  const actionCard = document.fields["#v2-action-list"].children[0];

  ui.syncPolling();
  await scheduled[0].fn();

  assert.equal(document.fields["#v2-action-list"].children[0], actionCard);
});

test("Profile bootstrap failure leaves non-Profile views available", async () => {
  const {ui, requests} = harness({
    responses: {
      "GET /api/browser-v2/profiles": response(500, {error: {message: "请求处理失败。"}}),
      "GET /api/browser-v2/elements": response(200, {data: [{id: "element-1"}]}),
      "GET /api/browser-v2/strategies": response(200, {data: [{id: "strategy-1"}]}),
      "GET /api/browser-v2/history": response(200, {data: [{id: "job-1"}]}),
    },
  });

  await ui.init();

  assert.equal(ui.state.profilesAvailable, false);
  assert.equal(ui.state.status, "部分可用：AdsPower 未连接");
  assert.equal(
    ui.state.error,
    "无法读取 AdsPower Profile，请确认 AdsPower 已启动及 API Key 正确",
  );
  assert.deepEqual(ui.state.elements, [{id: "element-1"}]);
  assert.deepEqual(ui.state.strategies, [{id: "strategy-1"}]);
  assert.deepEqual(ui.state.history, [{id: "job-1"}]);
  assert.equal(ui.switchView("elements"), true);
  assert.equal(ui.switchView("strategies"), true);
  assert.equal(ui.switchView("history"), true);
  assert.equal(ui.switchView("settings"), true);
  assert.equal(await ui.startJob(), false);
  assert.equal(await ui.startPicker(), false);
  assert.equal(requests.some((item) => item.method === "POST"), false);
});

test("successful bootstrap reports ready and enables Profile actions", async () => {
  const {ui} = harness({
    responses: {
      "GET /api/browser-v2/profiles": response(200, {data: []}),
      "GET /api/browser-v2/elements": response(200, {data: []}),
      "GET /api/browser-v2/strategies": response(200, {data: []}),
      "GET /api/browser-v2/history": response(200, {data: []}),
    },
  });

  await ui.init();

  assert.equal(ui.state.profilesAvailable, true);
  assert.equal(ui.state.status, "就绪");
  assert.equal(ui.state.error, "");
});

test("bootstrap loads closed V2 content-library metadata", async () => {
  const {ui, requests} = harness({
    responses: {
      "GET /api/browser-v2/content-libraries": response(200, {data: [
        {id: "ofs", name: "OFS", copy_count: 40},
      ]}),
    },
  });

  await ui.init();

  assert.deepEqual(ui.state.contentLibraries, [
    {id: "ofs", name: "OFS", copy_count: 40},
  ]);
  assert.equal(
    requests.some((item) => item.url === "/api/browser-v2/content-libraries"),
    true,
  );
});

test("input action shows strict target guidance and library source controls", () => {
  const document = strategyDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  const action = actionTemplate("input", "input-1");
  action.content_source = "library";
  action.content_library_id = "ofs";
  ui.state.contentLibraries = [
    {id: "ofs", name: "OFS", copy_count: 40},
    {id: "empty", name: "Empty", copy_count: 0},
  ];
  ui.state.draft = {
    id: "strategy-1", name: "Comment", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: [action]},
  };

  ui.render();

  const card = document.fields["#v2-action-list"].children[0];
  const text = descendants(card).map((item) => item.textContent || "").join(" ");
  assert.match(text, /<input>/);
  assert.match(text, /<textarea>/);
  assert.match(text, /\[contenteditable=true\]/);
  assert.match(text, /kind=input/);
  const options = descendants(card, "option");
  assert.equal(options.find((option) => option.value === "ofs").selected, true);
  assert.equal(options.find((option) => option.value === "empty").disabled, true);
});

test("library input action serializes selected target and source", async () => {
  const document = strategyDocument();
  const fields = document.fields;
  fields["#v2-strategy-name"].value = "Comment";
  fields["#v2-strategy-target-url"].value = "https://www.tiktok.com/";
  fields["#v2-strategy-ready-element"].value = "ready-1";
  fields["#v2-strategy-readiness-timeout"].value = "15";
  fields["#v2-strategy-run-mode"].value = "once";
  let received;
  const ui = createBrowserV2UI({
    document,
    requestJson: async (url, method, body) => {
      if (url === "/api/browser-v2/strategies" && method === "POST") {
        received = body;
        return response(422, {error: {message: "stop after capture"}});
      }
      return response(200, {data: []});
    },
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-1", localNew: true, name: "Comment", enabled: true,
    definition: {
      target_url: "https://www.tiktok.com/", ready_element_id: "ready-1",
      readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null,
      actions: [{
        id: "input-1", type: "input", element_id: "comment-input",
        content_source: "library", fixed_text: "old fixed text",
        content_library_id: "ofs", interval_ms: [40, 120],
      }],
    },
  };

  assert.equal(await ui.saveStrategy(), false);
  assert.deepEqual(received.definition.actions[0], {
    id: "input-1", type: "input", element_id: "comment-input",
    content_source: "library", fixed_text: "", content_library_id: "ofs",
    interval_ms: [40, 120],
  });
});

test("real browser response envelope initializes public Profile list", async () => {
  const calls = [];
  const ui = createBrowserV2UI(browserV2Dependencies(browserWindow(async (url) => {
    calls.push(url);
    const values = {
      "/api/browser-v2/profiles": [{profile_token: "profile_token_a", display_id: "***001", name: "测试窗口", status: "ready"}],
      "/api/browser-v2/elements": [], "/api/browser-v2/strategies": [], "/api/browser-v2/history": [],
    };
    return {status: 200, json: async () => ({data: values[url]})};
  })));
  await ui.init();
  assert.deepEqual(ui.state.profiles, [{profile_token: "profile_token_a", display_id: "***001", name: "测试窗口", status: "ready"}]);
  assert.equal(calls.includes("/api/browser-v2/profiles"), true);
});

test("active picker keeps selected Profile locked when status omits profile_token", () => {
  const document = pickerDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.profilesAvailable = true;
  ui.state.profiles = [{profile_token: "profile_token_a", display_id: "***001"}];
  ui.state.pickerProfileToken = "profile_token_a";
  ui.state.picker = {id: "picker-1", status: "waiting_for_selection"};

  ui.render();

  const select = document.fields["#v2-picker-profile"];
  assert.equal(select.children.find((option) => option.selected)?.value, "profile_token_a");
  assert.equal(select.disabled, true);

  ui.state.picker = {id: "picker-1", status: "completed"};
  ui.render();
  assert.equal(select.children.find((option) => option.selected)?.value, "profile_token_a");
  assert.equal(select.disabled, false);
});

test("polling the same picker selection preserves name input node and value", () => {
  const document = pickerDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.profilesAvailable = true;
  ui.state.picker = {
    id: "picker-1", status: "selection_ready",
    selection: {tag: "button", actionable_ancestor_fingerprint: "button-1"},
  };

  ui.render();
  const form = document.fields["#v2-picker-candidates"].children[0];
  const name = form.children[0];
  name.value = "comment entry";
  ui.state.picker = {
    id: "picker-1", status: "selection_ready",
    selection: {tag: "button", actionable_ancestor_fingerprint: "button-1"},
  };
  ui.render();

  assert.equal(document.fields["#v2-picker-candidates"].children[0], form);
  assert.equal(form.children[0], name);
  assert.equal(name.value, "comment entry");

  ui.state.picker.selection = {tag: "button", actionable_ancestor_fingerprint: "button-2"};
  ui.render();
  assert.notEqual(document.fields["#v2-picker-candidates"].children[0], form);
  assert.equal(document.fields["#v2-picker-candidates"].children[0].children[0].value, "");
});

test("picker save failure keeps candidate form for retry", async () => {
  const document = pickerDocument();
  const ui = createBrowserV2UI({
    document,
    requestJson: async () => response(422, {error: {message: "save failed"}}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.picker = {
    id: "picker-1", status: "selection_ready",
    selection: {tag: "button", actionable_ancestor_fingerprint: "button-1"},
  };
  ui.render();
  const form = document.fields["#v2-picker-candidates"].children[0];
  form.children[0].value = "comment entry";

  assert.equal(await ui.savePickerElement("comment entry", "action", "click"), false);
  ui.render();
  assert.equal(document.fields["#v2-picker-candidates"].children[0], form);
  assert.equal(form.children[0].value, "comment entry");
});

test("picker save success clears candidate form for the next selection", async () => {
  const document = pickerDocument();
  const ui = createBrowserV2UI({
    document,
    requestJson: async (url, method) => method === "POST"
      ? response(201, {data: {id: "element-1"}})
      : response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.picker = {
    id: "picker-1", status: "selection_ready",
    selection: {tag: "button", actionable_ancestor_fingerprint: "button-1"},
  };
  ui.render();

  assert.equal(await ui.savePickerElement("comment entry", "action", "click"), true);
  assert.equal(document.fields["#v2-picker-candidates"].childElementCount, 0);
  assert.equal(ui.state.picker.status, "waiting_for_selection");
});

test("real browser start envelope converts job_id then polls its exact job route", async () => {
  const calls = [], timers = [];
  const fields = {
    "#v2-run-strategy": {value: "strategy-1"}, "#v2-run-batch-size": {value: "3"}, "#v2-run-start": {disabled: false},
  };
  let enabled = true;
  const window = browserWindow(async (url, options) => {
    calls.push({url, options});
    if (url === "/api/browser-v2/jobs" && options.method === "POST") { enabled = false; return {status: 202, json: async () => ({data: {job_id: "job-9"}})}; }
    if (url === "/api/browser-v2/jobs/job-9") return {status: 200, json: async () => ({data: {job_id: "job-9", status: "completed"}})};
    return {status: 200, json: async () => ({data: []})};
  });
  window.setTimeout = (fn, delay) => { const timer = {fn, delay}; timers.push(timer); return timer; };
  window.document = {querySelector: (selector) => enabled ? fields[selector] || null : null, querySelectorAll: (selector) => enabled && selector === "#v2-profile-list input:checked" ? [{value: "profile_token_a"}] : []};
  const ui = createBrowserV2UI(browserV2Dependencies(window));
  ui.state.profilesAvailable = true;
  assert.equal(await ui.startJob(), true);
  assert.deepEqual(ui.state.job, {id: "job-9", status: "queued"});
  assert.equal(timers[0].delay, 1000);
  await timers[0].fn();
  assert.equal(calls.some((call) => call.url === "/api/browser-v2/jobs/job-9" && call.options.method === "GET"), true);
});

test("repick saves exact optimistic-revision body and clears target after HTTP success", async () => {
  let received;
  const {ui} = harness({responses: {
    "POST /api/browser-v2/picker/session-1/save": (body) => { received = body; return response(201, {data: {id: "element-1", revision: 4}}); },
    "GET /api/browser-v2/elements": response(200, {data: []}),
  }});
  ui.state.picker = {id: "session-1", status: "selection_ready", selection: {tag: "button"}};
  ui.beginRepick({id: "element-1", revision: 3, name: "评论入口", purpose: "action", kind: "click"});
  assert.equal(await ui.savePickerElement("评论入口", "action", "click"), true);
  assert.deepEqual(received, {name: "评论入口", purpose: "action", kind: "click", element_id: "element-1", expected_revision: 3});
  assert.equal(ui.state.repickTarget, null);
});

test("evidence links accept one safe relative PNG path only", () => {
  assert.equal(safeEvidencePath("evidence/abc-123.png"), "evidence/abc-123.png");
  ["javascript:alert(1)", "https://example.test/a.png", "evidence/../secret.png", "evidence/a.svg", "/evidence/a.png"].forEach((value) => assert.equal(safeEvidencePath(value), ""));
});

test("Profile bulk controls select and clear every token checkbox", () => {
  const inputs = [{value: "profile_token_a", checked: false}, {value: "profile_token_b", checked: false}];
  const document = {querySelector: () => null, querySelectorAll: (selector) => selector === "#v2-profile-list input[type='checkbox']" ? inputs : []};
  const ui = createBrowserV2UI({document, requestJson: async () => response(200, {data: []}), setTimeout: () => 1, clearTimeout: () => {}});
  ui.setAllProfiles(true); assert.deepEqual(inputs.map((input) => input.checked), [true, true]);
  ui.setAllProfiles(false); assert.deepEqual(inputs.map((input) => input.checked), [false, false]);
  const html = fs.readFileSync(path.join(__dirname, "..", "gateway", "templates", "browser_v2.html"), "utf8");
  assert.match(html, /id="v2-profile-select-all"/); assert.match(html, /id="v2-profile-clear-all"/);
});

test("strategy save sends only the closed V2 schema after form editing", async () => {
  const fields = {
    "#v2-strategy-name": {value: "评论动作"},
    "#v2-strategy-target-url": {value: "https://www.tiktok.com/"},
    "#v2-strategy-ready-element": {value: "ready-1"},
    "#v2-strategy-readiness-timeout": {value: "15"},
    "#v2-strategy-run-mode": {value: "once"},
    "#v2-strategy-minutes": {value: ""},
    "#v2-strategy-enabled": {checked: true},
  };
  let enabled = false;
  const document = {querySelector: (selector) => enabled ? fields[selector] || null : null, querySelectorAll: () => []};
  let received;
  const ui = createBrowserV2UI({
    document,
    requestJson: async (url, method, body) => {
      if (method === "POST" && url === "/api/browser-v2/strategies") { received = body; return response(422, {error: {code: "validation_failed", message: "元素尚未保存"}}); }
      return response(200, {data: []});
    },
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-new", localNew: true, name: "旧名", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "ready-1", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: [{id: "scroll-1", type: "scroll", direction: "down", distance_pixels: [400, 600], count: [2, 3], interval_seconds: [0.2, 0.5]}]},
  };
  enabled = true;
  assert.equal(await ui.saveStrategy(), false);
  assert.deepEqual(received, {
    id: "strategy-new", name: "评论动作", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "ready-1", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: [{id: "scroll-1", type: "scroll", direction: "down", distance_pixels: [120, 120], count: [2, 3], interval_seconds: [0.2, 0.5]}]},
  });
  assert.equal(ui.state.error, "元素尚未保存");
  assert.equal(Object.hasOwn(received, "actions"), false);
});

test("run submit uses opaque profile tokens and bounded batch payload", async () => {
  const fields = {
    "#v2-run-strategy": {value: "strategy-1"},
    "#v2-run-batch-size": {value: "3"},
    "#v2-run-start": {disabled: false},
  };
  let enabled = false;
  let received;
  const document = {
    querySelector: (selector) => enabled ? fields[selector] || null : null,
    querySelectorAll: (selector) => enabled && selector === "#v2-profile-list input:checked" ? [{value: "profile_token_a"}, {value: "profile_token_b"}] : [],
  };
  const ui = createBrowserV2UI({
    document,
    requestJson: async (url, method, body) => {
      if (method === "POST" && url === "/api/browser-v2/jobs") { received = body; enabled = false; return response(422, {error: {code: "validation_failed", message: "策略未就绪"}}); }
      return response(200, {data: []});
    },
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  enabled = true;
  ui.state.profilesAvailable = true;
  assert.equal(await ui.startJob(), false);
  assert.deepEqual(received, {strategy_id: "strategy-1", profile_tokens: ["profile_token_a", "profile_token_b"], batch_size: 3});
  assert.equal(ui.state.error, "策略未就绪");
});

test("Chinese stages are specific and never expose internal fallback fields", () => {
  assert.equal(stageLabel("navigating"), "正在打开页面");
  assert.equal(stageLabel("completed"), "已完成");
  assert.equal(stageLabel("not-a-real-stage"), "处理中");
});
