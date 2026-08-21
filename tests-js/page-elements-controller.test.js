const assert = require("node:assert/strict");
const test = require("node:test");

const {createPageElementsController, safeEvidencePath} = require("../gateway/static/page_elements_controller");

function response(status, data) { return {status, data: data || {}}; }

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

function pageElementsDocument() {
  const fields = {
    "#v2-picker-profile": fakeNode("select"),
    "#v2-picker-url": fakeNode("input"),
    "#v2-picker-start": fakeNode("button"),
    "#v2-picker-finish": fakeNode("button"),
    "#v2-picker-cancel": fakeNode("button"),
    "#v2-picker-state": fakeNode(),
    "#v2-picker-candidates": fakeNode(),
    "#v2-element-validate-profile": fakeNode("select"),
    "#v2-elements-search": fakeNode("input"),
    "#v2-elements-kind-filter": fakeNode("select"),
    "#v2-elements-status-filter": fakeNode("select"),
    "#v2-elements-list": fakeNode(),
    "#v2-elements-empty": fakeNode(),
  };
  return {
    fields,
    createElement: (tag) => fakeNode(tag),
    querySelector: (selector) => fields[selector] || null,
    querySelectorAll: () => [],
  };
}

function controllerHarness(overrides = {}) {
  const document = overrides.document || pageElementsDocument();
  const requests = [];
  const scheduled = [];
  const cleared = [];
  const unload = [];
  const controller = createPageElementsController({
    root: document,
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      const item = overrides.responses?.[`${method || "GET"} ${url}`];
      return typeof item === "function" ? item(body) : (item || response(200, {data: []}));
    },
    setTimeout: (fn, delay) => { const token = {fn, delay}; scheduled.push(token); return token; },
    clearTimeout: (token) => cleared.push(token),
    addBeforeUnload: (handler) => unload.push(handler),
    removeBeforeUnload: (handler) => unload.splice(unload.indexOf(handler), 1),
    confirm: overrides.confirm || (() => true),
    prompt: overrides.prompt,
    onMessage: overrides.onMessage,
    onElementsChanged: overrides.onElementsChanged,
  });
  return {controller, document, requests, scheduled, cleared, unload};
}

test("controller loads profiles and elements and publishes an isolated snapshot", async () => {
  const changed = [];
  const {controller} = controllerHarness({
    responses: {
      "GET /api/browser-v2/profiles": response(200, {data: [{profile_token: "p1", name: "One"}]}),
      "GET /api/browser-v2/elements": response(200, {data: [{id: "e1", name: "Like", revision: 1, status: "active"}]}),
    },
    onElementsChanged: (items) => changed.push(items),
  });

  await controller.init();

  assert.equal(controller.getElements()[0].id, "e1");
  assert.equal(changed.at(-1)[0].name, "Like");
  changed.at(-1)[0].name = "mutated";
  assert.equal(controller.getElements()[0].name, "Like");
});

test("initial load failure is reported instead of a success message", async () => {
  const messages = [];
  const {controller} = controllerHarness({
    responses: {
      "GET /api/browser-v2/profiles": response(503, {error: "offline"}),
      "GET /api/browser-v2/elements": response(503, {error: "offline"}),
    },
    onMessage: (message) => messages.push(message),
  });

  await controller.init();

  assert.equal(controller.state.elementsLoaded, false);
  assert.equal(messages.at(-1).error, true);
  assert.match(messages.at(-1).text, /加载失败/);
  assert.doesNotMatch(messages.at(-1).text, /已更新/);
});

test("stale revision is reported without reloading over the local operation", async () => {
  const messages = [];
  const {controller, requests} = controllerHarness({
    responses: {
      "PUT /api/browser-v2/elements/e1": response(409, {error: {message: "revision conflict"}}),
    },
    prompt: () => "New",
    onMessage: (message) => messages.push(message),
  });
  controller.state.elements = [{id: "e1", name: "Like", revision: 2, status: "active"}];
  controller.state.initialized = true;

  const saved = await controller.renameElement(controller.state.elements[0], "New");

  assert.equal(saved, false);
  assert.match(messages.at(-1).text, /版本已变化/);
  assert.equal(requests.filter((item) => item.url === "/api/browser-v2/elements").length, 0);
});

test("picker polling is separate and only reads the active picker", async () => {
  const {controller, scheduled, requests} = controllerHarness({
    responses: {
      "GET /api/browser-v2/picker/p1": response(200, {data: {id: "p1", status: "completed"}}),
    },
  });
  controller.state.picker = {id: "p1", status: "waiting_for_selection"};
  controller.syncPolling();

  assert.equal(scheduled.length, 1);
  await scheduled[0].fn();

  assert.deepEqual(requests.map((item) => item.url), ["/api/browser-v2/picker/p1"]);
  assert.equal(controller.hasActivePicker(), false);
});

test("destroy stops polling and removes the beforeunload guard", async () => {
  const {controller, scheduled, cleared, unload} = controllerHarness();
  await controller.init();
  controller.state.picker = {id: "p1", status: "waiting_for_selection"};
  controller.syncPolling();
  assert.equal(scheduled.length, 1);
  assert.equal(unload.length, 1);

  controller.destroy();

  assert.deepEqual(cleared, [scheduled[0]]);
  assert.equal(unload.length, 0);
});

test("delete requires confirmation before issuing the destructive request", async () => {
  let confirmed = false;
  const {controller, requests} = controllerHarness({confirm: () => false});
  controller.state.elements = [{id: "e1", name: "Like", revision: 2, status: "active"}];
  controller.state.initialized = true;
  const deleted = await controller.deleteElement(controller.state.elements[0]);

  assert.equal(deleted, false);
  assert.equal(confirmed, false);
  assert.equal(requests.length, 0);
});

test("console element workspace filters records and expands complete definition details", async () => {
  const {controller, document} = controllerHarness();
  controller.state.elements = [
    {
      id: "e1", name: "评论输入", purpose: "action", kind: "input", status: "active", revision: 4,
      created_at: "2026-08-18T10:00:00Z", updated_at: "2026-08-19T10:00:00Z",
      definition: {
        url_pattern: "https://www.tiktok.com/",
        frame_path: ["main", "iframe[name=comment]"],
        locators: [{type: "css", value: "textarea[data-e2e=comment]", priority: 0}],
        diagnostic_metadata: {tag: "textarea", role: "textbox", text_preview: "评论"},
        screenshot_path: "evidence/0123456789abcdef0123456789abcdef.png",
      },
    },
    {id: "e2", name: "赞按钮", purpose: "action", kind: "click", status: "active", revision: 1},
  ];
  controller.state.profilesAvailable = true;
  controller.state.initialized = true;
  controller.state.latestValidation.set("e1", {valid: true, diagnostics: [{locator_index: 0}]});

  controller.setFilters({query: "评论"});
  assert.equal(document.fields["#v2-elements-list"].children.length, 1);

  controller.toggleDetails("e1");
  const text = descendants(document.fields["#v2-elements-list"]).map((item) => item.textContent || "").join(" ");
  assert.match(text, /https:\/\/www\.tiktok\.com/);
  assert.match(text, /iframe\[name=comment\]/);
  assert.match(text, /textarea\[data-e2e=comment\]/);
  assert.match(text, /0123456789abcdef0123456789abcdef\.png/);
  assert.match(text, /元素 ID e1/);
  assert.match(text, /版本 4/);
  assert.match(text, /创建时间 2026-08-18T10:00:00Z/);
  assert.match(text, /更新时间 2026-08-19T10:00:00Z/);
  assert.match(text, /定位器数量 1/);
  assert.match(text, /最近校验结果 校验通过/);
  assert.match(text, /locator_index/);
  assert.equal(safeEvidencePath("evidence/0123456789abcdef0123456789abcdef.png"), "/evidence/0123456789abcdef0123456789abcdef.png");
  assert.equal(safeEvidencePath("evidence/not-safe.png"), "");
});

function descendants(node) {
  return [node, ...(node.children || []).flatMap((child) => descendants(child))];
}
