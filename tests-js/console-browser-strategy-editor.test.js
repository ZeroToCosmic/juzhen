"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const core = require("../gateway/static/browser_strategy_editor_core");
const editor = require("../gateway/static/console_browser_strategy_editor");

class FakeElement {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.checked = false;
    this.textContent = "";
    this.className = "";
    this.name = "";
    this.type = "";
    this.options = [];
  }
  append(...nodes) {
    for (const node of nodes.flat()) {
      if (node == null || typeof node === "string") continue;
      node.parentNode = this;
      this.children.push(node);
      if (node.tagName === "OPTION") this.options.push(node);
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { this.children = []; this.options = []; this.append(...nodes); }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  dispatch(type, extra = {}) {
    const event = {type, target: this, preventDefault() {}, ...extra};
    for (const handler of this.listeners[type] || []) handler(event);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  closest() { return this; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  contains() { return false; }
}

function createDocument() {
  const ids = [
    "console-browser-strategy-editor", "strategy-revision", "strategy-enabled", "strategy-save",
    "strategy-name", "strategy-target-url", "strategy-ready-element", "strategy-readiness-timeout",
    "strategy-run-mode", "strategy-minutes-wrap", "strategy-minutes", "strategy-action-palette",
    "strategy-action-list", "strategy-action-empty", "strategy-copy", "strategy-delete", "strategy-status",
    "console-browser-strategy-bootstrap",
  ];
  const nodes = new Map(ids.map((id) => [id, new FakeElement("div", id)]));
  nodes.get("strategy-enabled").tagName = "INPUT";
  nodes.get("strategy-enabled").type = "checkbox";
  nodes.get("strategy-save").tagName = "BUTTON";
  nodes.get("strategy-copy").tagName = "BUTTON";
  nodes.get("strategy-delete").tagName = "BUTTON";
  nodes.get("console-browser-strategy-bootstrap").textContent = JSON.stringify({mode: "new", strategy_id: null});
  const document = {
    readyState: "complete",
    getElementById(id) { return nodes.get(id) || null; },
    querySelector(selector) {
      if (selector === "#console-browser-strategy-editor") return nodes.get("console-browser-strategy-editor");
      if (selector === "#console-browser-strategy-bootstrap") return nodes.get("console-browser-strategy-bootstrap");
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-action-type]") return [];
      return [];
    },
    createElement(tagName) { return new FakeElement(tagName); },
    addEventListener() {},
  };
  return {document, nodes};
}

function record(id = "strategy-1", revision = 3) {
  return {
    id, name: "Feed", enabled: true, revision,
    definition: {
      target_url: "https://www.tiktok.com/",
      ready_element_id: "ready-1", readiness_timeout_seconds: 15,
      run_mode: "once", loop_duration_minutes: null,
      actions: [core.actionTemplate("wait", "wait-1")],
    },
  };
}

function deps() {
  return [[
    {id: "ready-1", name: "Ready", purpose: "readiness", kind: "generic", status: "active"},
    {id: "click-1", name: "Click", purpose: "action", kind: "click", status: "active"},
    {id: "input-1", name: "Input", purpose: "action", kind: "input", status: "active"},
  ], [{id: "library-1", name: "Captions", copy_count: 4}]];
}

function makeController(overrides = {}) {
  const {document, nodes} = createDocument();
  const calls = [];
  const history = {replaceState(...args) { calls.push({kind: "replaceState", args}); }};
  const location = {assign(url) { calls.push({kind: "assign", url}); }};
  const repository = Object.assign({
    loadDependencies: async () => { calls.push({kind: "loadDependencies"}); return deps(); },
    load: async (id) => { calls.push({kind: "load", id}); return record(id); },
    create: async (draft) => { calls.push({kind: "create", draft: core.buildCreatePayload(draft)}); return record(draft.id, 1); },
    update: async (draft) => { calls.push({kind: "update", draft: core.buildUpdatePayload(draft)}); return record(draft.id, 4); },
    remove: async (draft) => { calls.push({kind: "remove", draft}); return {}; },
  }, overrides.repository || {});
  const controller = editor.createConsoleStrategyEditor({
    ...overrides, document, repository, history, location, confirm: overrides.confirm || (() => true),
    idFactory: overrides.idFactory || (() => "copy-id"),
  });
  return {controller, nodes, calls};
}

test("exports UMD controller and canonical URL encodes strategy IDs", () => {
  assert.equal(typeof editor.createConsoleStrategyEditor, "function");
  assert.equal(typeof editor.boot, "function");
  assert.equal(editor.canonicalEditUrl("中文 strategy/1"), "/console/actions/browser-strategies/%E4%B8%AD%E6%96%87%20strategy%2F1/edit");
});

test("new mode only loads dependencies and never touches unrelated endpoints", async () => {
  const {controller, calls} = makeController();
  await controller.init();
  assert.deepEqual(calls.map((item) => item.kind), ["loadDependencies"]);
  assert.equal(controller.state.draft.localNew, true);
  assert.equal(controller.state.draft.id, "copy-id");
  assert.equal(controller.state.elements.length, 3);
});

test("edit mode loads exactly dependencies plus the encoded strategy", async () => {
  const {controller, calls} = makeController({bootstrap: {mode: "edit", strategy_id: "中文 strategy/1"}});
  await controller.init();
  assert.deepEqual(calls.map((item) => item.kind), ["loadDependencies", "load"]);
  assert.equal(calls[1].id, "中文 strategy/1");
  assert.equal(controller.state.draft.id, "中文 strategy/1");
});

test("initial load failure disables the editor and exposes a reload entry point", async () => {
  let dependencyAttempts = 0;
  const {controller, nodes, calls} = makeController({
    repository: {
      loadDependencies: async () => {
        calls.push({kind: "loadDependencies"});
        dependencyAttempts += 1;
        if (dependencyAttempts === 1) throw new Error("元素服务不可用");
        return deps();
      },
    },
  });

  await controller.init();

  assert.equal(controller.state.loading, false);
  assert.equal(controller.state.loadFailed, true);
  assert.equal(controller.state.initialized, false);
  assert.match(controller.state.status, /加载策略失败：元素服务不可用/);
  assert.match(nodes.get("console-browser-strategy-editor").className, /is-disabled/);
  assert.match(nodes.get("console-browser-strategy-editor").className, /is-error/);
  assert.equal(nodes.get("strategy-save").textContent, "重新加载");
  assert.equal(nodes.get("strategy-save").disabled, false);
  assert.equal(nodes.get("strategy-copy").hidden, true);
  assert.equal(nodes.get("strategy-delete").hidden, true);
  assert.equal(nodes.get("strategy-action-palette").disabled, true);
  assert.equal(nodes.get("strategy-name").disabled, true);
  assert.deepEqual(calls.map((item) => item.kind), ["loadDependencies"]);

  nodes.get("strategy-save").dispatch("click");
  await controller.init();

  assert.equal(controller.state.loading, false);
  assert.equal(controller.state.loadFailed, false);
  assert.equal(controller.state.initialized, true);
  assert.equal(controller.state.draft.localNew, true);
  assert.equal(nodes.get("strategy-save").textContent, "保存策略");
  assert.equal(nodes.get("strategy-save").disabled, false);
  assert.equal(nodes.get("strategy-action-palette").disabled, false);
  assert.equal(nodes.get("strategy-name").disabled, false);
  assert.equal(nodes.get("console-browser-strategy-editor").className.includes("is-error"), false);
  assert.deepEqual(calls.map((item) => item.kind), ["loadDependencies", "loadDependencies"]);
  assert.equal(nodes.get("strategy-save").listeners.click.length, 1);
});

test("edit load failure retries the strategy request without unrelated endpoints", async () => {
  let strategyAttempts = 0;
  const {controller, nodes, calls} = makeController({
    bootstrap: {mode: "edit", strategy_id: "strategy-1"},
    repository: {
      load: async (id) => {
        strategyAttempts += 1;
        calls.push({kind: "load", id});
        if (strategyAttempts === 1) throw new Error("策略读取失败");
        return record(id);
      },
    },
  });

  await controller.init();
  assert.equal(controller.state.loadFailed, true);
  assert.equal(nodes.get("strategy-copy").hidden, true);
  assert.equal(nodes.get("strategy-delete").hidden, true);

  nodes.get("strategy-save").dispatch("click");
  await controller.init();

  assert.equal(controller.state.loadFailed, false);
  assert.equal(controller.state.draft.id, "strategy-1");
  assert.equal(nodes.get("strategy-copy").hidden, false);
  assert.equal(nodes.get("strategy-delete").hidden, false);
  assert.deepEqual(calls.map((item) => item.kind), [
    "loadDependencies", "load", "loadDependencies", "load",
  ]);
  assert.equal(nodes.get("strategy-save").listeners.click.length, 1);
});

test("renders five action cards with the CSS contract and fields container", async () => {
  const {controller, nodes} = makeController();
  await controller.init();
  for (const type of ["move", "scroll", "click", "input", "wait"]) core.addAction(controller.state.draft, type);
  controller.render();
  const cards = nodes.get("strategy-action-list").children;
  assert.equal(cards.length, 5);
  for (const card of cards) {
    assert.equal(card.className, "strategy-action-card");
    assert.equal(card.children[1].className, "strategy-action-fields");
    assert.ok(card.children[1].children.length > 0);
  }
});

test("browser adapter uses same-origin fetch and JSON only for mutating calls", async () => {
  const {document} = createDocument();
  const fetchCalls = [];
  const browserRoot = {
    document,
    history: {replaceState() {}},
    location: {assign() {}},
    confirm: () => true,
    fetch: async (url, options) => {
      fetchCalls.push({url, options});
      if (url.endsWith("/elements")) return {status: 200, json: async () => ({data: deps()[0]})};
      if (url.endsWith("/content-libraries")) return {status: 200, json: async () => ({data: deps()[1]})};
      return {status: 201, json: async () => ({data: record("server-id", 1)})};
    },
  };
  const controller = editor.createConsoleStrategyEditor({document, root: browserRoot, bootstrap: {mode: "new", strategy_id: ""}});
  await controller.init();
  controller.state.draft.definition.ready_element_id = "ready-1";
  document.getElementById("strategy-name").value = "Adapter strategy";
  document.getElementById("strategy-ready-element").value = "ready-1";
  await controller.save();
  assert.deepEqual(fetchCalls.map((call) => [call.options.method, call.url]), [
    ["GET", "/api/browser-v2/elements"],
    ["GET", "/api/browser-v2/content-libraries"],
    ["POST", "/api/browser-v2/strategies"],
  ]);
  assert.equal(fetchCalls[0].options.body, undefined);
  assert.equal(fetchCalls[2].options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(fetchCalls[2].options.body).definition.actions, []);
});

test("create sends complete normalized payload and replaces the URL", async () => {
  const {controller, nodes, calls} = makeController();
  await controller.init();
  const draft = controller.state.draft;
  draft.name = "New strategy";
  draft.definition.ready_element_id = "ready-1";
  draft.definition.readiness_timeout_seconds = "30";
  draft.definition.actions = [{...core.actionTemplate("click", "click-1"), element_id: "click-1", click_count: "2", hold_seconds: "0.05-0.1", after_seconds: "0.3-0.6"}];
  nodes.get("strategy-name").value = draft.name;
  nodes.get("strategy-target-url").value = draft.definition.target_url;
  nodes.get("strategy-ready-element").value = "ready-1";
  nodes.get("strategy-readiness-timeout").value = "30";
  await controller.save();
  const call = calls.find((item) => item.kind === "create");
  assert.equal(call.draft.id, "copy-id");
  assert.equal(call.draft.definition.readiness_timeout_seconds, 30);
  assert.equal(call.draft.definition.actions[0].click_count, 2);
  assert.equal(calls.at(-1).kind, "replaceState");
  assert.equal(calls.at(-1).args[2], editor.canonicalEditUrl(call.draft.id));
});

test("update sends full normalized payload and refreshes revision", async () => {
  const {controller, nodes, calls} = makeController({bootstrap: {mode: "edit", strategy_id: "strategy 1"}});
  await controller.init();
  nodes.get("strategy-readiness-timeout").value = "45";
  await controller.save();
  const call = calls.find((item) => item.kind === "update");
  assert.deepEqual(Object.keys(call.draft).sort(), ["definition", "enabled", "expected_revision", "name"].sort());
  assert.equal(call.draft.definition.readiness_timeout_seconds, 45);
  assert.equal(controller.state.draft.revision, 4);
});

test("409 keeps the draft and URL while showing the reload message", async () => {
  const error = new core.StrategyRequestError("revision_conflict", "conflict", 409);
  const {controller, calls} = makeController({bootstrap: {mode: "edit", strategy_id: "strategy-1"}, repository: {update: async () => { throw error; }}});
  await controller.init();
  const before = controller.state.draft.revision;
  await controller.save();
  assert.equal(controller.state.draft.revision, before);
  assert.equal(calls.some((item) => item.kind === "replaceState"), false);
  assert.match(controller.state.status, /数据已更新，请重新加载/);
});

test("copy creates a fresh persisted ID and navigates to its encoded edit URL", async () => {
  const {controller, calls} = makeController({bootstrap: {mode: "edit", strategy_id: "strategy-1"}});
  await controller.init();
  await controller.copy();
  const call = calls.find((item) => item.kind === "create");
  assert.equal(call.draft.id, "copy-id");
  assert.equal(calls.at(-1).kind, "replaceState");
  assert.equal(calls.at(-1).args[2], editor.canonicalEditUrl("copy-id"));
});

test("delete confirms and navigates back to the action library", async () => {
  let confirmed = 0;
  const {controller, calls} = makeController({bootstrap: {mode: "edit", strategy_id: "strategy-1"}, confirm: () => { confirmed += 1; return true; }});
  await controller.init();
  await controller.remove();
  assert.equal(confirmed, 1);
  assert.deepEqual(calls.at(-1), {kind: "assign", url: "/console/actions"});
});
