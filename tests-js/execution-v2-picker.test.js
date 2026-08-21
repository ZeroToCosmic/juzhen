const assert = require("node:assert/strict");
const test = require("node:test");

const { createPickerOverlay, resolveActionable, resolveEditableTarget, uniqueCss, xpathLiteral } = require("../execution_v2/picker_overlay.js");

function target(tagName, parent = null) {
  return {
    nodeType: 1,
    tagName,
    parentElement: parent,
    getBoundingClientRect: () => ({x: 10, y: 20, width: 30, height: 40}),
    getAttribute: (name) => (name === "role" && tagName === "DIV" ? "button" : null),
    matches: (selector) => selector === "button, a, input, textarea, select, [role='button'], [contenteditable='true']" && tagName === "BUTTON",
  };
}

function fakeElement(tagName = "DIV") {
  const attributes = new Map();
  const listeners = new Map();
  return {
    nodeType: 1, tagName, style: {}, children: [], attributes, listeners, removed: false,
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.get(name) || null; },
    addEventListener(name, handler) { listeners.set(name, handler); },
    remove() { this.removed = true; },
  };
}

function domElement(tagName = "DIV", values = {}, children = []) {
  const attributes = new Map(Object.entries(values));
  const item = {
    nodeType: 1, tagName, children, isConnected: true,
    getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
    getBoundingClientRect: () => ({x: 10, y: 20, width: 120, height: 40}),
    matches: () => false,
    querySelectorAll(selector) {
      if (selector !== "input, textarea, [contenteditable]") return [];
      const found = [];
      function visit(node) {
        for (const child of node.children || []) {
          const tag = String(child.tagName || "").toLowerCase();
          if (tag === "input" || tag === "textarea" || child.getAttribute("contenteditable") !== null) found.push(child);
          visit(child);
        }
      }
      visit(this);
      return found;
    },
  };
  children.forEach((child) => { child.parentElement = item; });
  return item;
}

function overlayHarness() {
  const emitted = [];
  const listeners = new Map();
  const appended = [];
  const document = {
    body: {appendChild: (node) => { appended.push(node); return node; }},
    createElement: (tag) => fakeElement(tag.toUpperCase()),
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: (name) => listeners.delete(name),
    querySelectorAll: () => [],
  };
  const overlay = createPickerOverlay({document, emit: (event) => emitted.push(event)});
  return {document, overlay, emitted, listeners, appended};
}

function eventFor(path, key = "") {
  const calls = {prevented: 0, stopped: 0};
  return {
    key, calls, composedPath: () => path,
    preventDefault: () => { calls.prevented += 1; },
    stopPropagation: () => { calls.stopped += 1; },
  };
}

test("picker resolves original SVG click to nearest actionable button ancestor", () => {
  const button = target("BUTTON");
  const svg = target("SVG", button);
  assert.equal(resolveActionable([svg, button]), button);
});

test("picker selects plaintext-only editable ancestor for an inner click", () => {
  const editor = domElement("DIV", {contenteditable: "plaintext-only"});
  const span = domElement("SPAN");
  span.parentElement = editor;

  assert.equal(resolveEditableTarget([span, editor]), editor);
  assert.equal(resolveActionable([span, editor]), editor);
});

test("picker selects one editable descendant from an outer wrapper", () => {
  const editor = domElement("DIV", {contenteditable: ""});
  const wrapper = domElement("DIV", {role: "textbox"}, [editor]);

  assert.equal(resolveEditableTarget([wrapper]), editor);
});

test("outer-wrapper click emits the normalized editable descendant", () => {
  const editor = domElement("DIV", {contenteditable: ""});
  const wrapper = domElement("DIV", {role: "textbox"}, [editor]);
  const {overlay, emitted, listeners} = overlayHarness();
  overlay.install();

  listeners.get("click")(eventFor([wrapper]));

  assert.equal(overlay.highlighted(), editor);
  assert.equal(emitted[0].actionable_tag, "div");
  assert.equal(Object.hasOwn(emitted[0].attributes, "contenteditable"), true);
  assert.equal(emitted[0].attributes.contenteditable, "");
});

test("picker never guesses between two editable descendants", () => {
  const wrapper = domElement("DIV", {}, [
    domElement("TEXTAREA"), domElement("DIV", {contenteditable: "true"}),
  ]);

  assert.equal(resolveEditableTarget([wrapper]), null);
});

test("contenteditable false is not editable", () => {
  const blocked = domElement("DIV", {contenteditable: "false"});

  assert.equal(resolveEditableTarget([blocked]), null);
});

test("picker click re-resolves actionable node instead of using stale hover", () => {
  const {overlay, emitted, listeners} = overlayHarness();
  const stale = target("DIV");
  const replacementButton = target("BUTTON");
  const svg = target("SVG", replacementButton);
  overlay.install();

  listeners.get("pointermove")(eventFor([stale]));
  listeners.get("click")(eventFor([svg, replacementButton]));

  assert.equal(overlay.highlighted(), replacementButton);
  assert.equal(emitted.length, 1);
  assert.equal(emitted[0].original_tag, "svg");
  assert.equal(emitted[0].actionable_tag, "button");
});

test("picker defaults to select then interaction mode passes page clicks through", () => {
  const {overlay, emitted, listeners, appended} = overlayHarness();
  const button = target("BUTTON");
  overlay.install();
  overlay.install();
  assert.equal(overlay.mode(), "select");
  assert.equal(appended.length, 2);

  listeners.get("pointermove")(eventFor([button]));
  assert.equal(appended[0].style.display, "block");
  const selected = eventFor([button]);
  listeners.get("click")(selected);
  assert.equal(selected.calls.prevented, 1);
  assert.equal(selected.calls.stopped, 1);
  assert.equal(emitted.length, 1);

  const toolbar = appended.find((node) => node.getAttribute("data-execution-v2-picker-ui") === "toolbar");
  const interact = toolbar.children.find((node) => node.getAttribute("data-picker-mode") === "interact");
  const toolbarCapture = eventFor([interact, toolbar]);
  listeners.get("click")(toolbarCapture);
  interact.listeners.get("click")(toolbarCapture);
  assert.equal(overlay.mode(), "interact");
  assert.equal(appended[0].style.display, "none");
  assert.equal(emitted.length, 1);

  const passed = eventFor([button]);
  listeners.get("click")(passed);
  assert.deepEqual(passed.calls, {prevented: 0, stopped: 0});
  assert.equal(emitted.length, 1);

  const select = toolbar.children.find((node) => node.getAttribute("data-picker-mode") === "select");
  const selectToolbar = eventFor([select, toolbar]);
  listeners.get("click")(selectToolbar);
  select.listeners.get("click")(selectToolbar);
  listeners.get("pointermove")(eventFor([button]));
  listeners.get("click")(eventFor([button]));
  assert.equal(overlay.mode(), "select");
  assert.equal(emitted.length, 2);
});

test("F2 toggles picker mode and Escape still cancels", () => {
  const {overlay, emitted, listeners} = overlayHarness();
  overlay.install();
  const first = eventFor([], "F2");
  listeners.get("keydown")(first);
  assert.equal(overlay.mode(), "interact");
  assert.deepEqual(first.calls, {prevented: 1, stopped: 1});
  listeners.get("keydown")(eventFor([], "F2"));
  assert.equal(overlay.mode(), "select");
  listeners.get("keydown")(eventFor([], "Escape"));
  assert.equal(emitted.at(-1).type, "cancel");
  assert.equal(listeners.size, 0);
});

test("uninstall removes picker toolbar marker and resets mode", () => {
  const {overlay, listeners, appended} = overlayHarness();
  overlay.install();
  listeners.get("keydown")(eventFor([], "F2"));
  overlay.uninstall();
  assert.equal(appended.every((node) => node.removed), true);
  assert.equal(listeners.size, 0);
  assert.equal(overlay.mode(), "select");
});

test("overlay installs one listener set, highlights hover, uses composedPath, Escape cancels, and cleans up", () => {
  const events = [];
  const listeners = new Map();
  const document = {
    body: { appendChild: () => {} },
    createElement: () => ({ style: {}, remove: () => {}, setAttribute: () => {} }),
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: (name) => listeners.delete(name),
  };
  const button = target("BUTTON");
  const svg = target("SVG", button);
  const overlay = createPickerOverlay({ document, emit: (event) => events.push(event) });
  overlay.install();
  overlay.install();
  assert.equal(listeners.size, 3);
  listeners.get("pointermove")({ composedPath: () => [svg, button] });
  assert.equal(overlay.highlighted(), button);
  listeners.get("click")({ preventDefault: () => {}, stopPropagation: () => {}, composedPath: () => [svg, button] });
  assert.equal(events[0].original_tag, "svg");
  assert.equal(events[0].actionable_tag, "button");
  listeners.get("keydown")({ key: "Escape", preventDefault: () => {} });
  assert.equal(events.at(-1).type, "cancel");
  overlay.uninstall();
  assert.equal(listeners.size, 0);
});

test("Cypress unique selector dependency is installed and exposes its generator", () => {
  const unique = require("@cypress/unique-selector").default;
  assert.equal(typeof unique, "function");
});

test("overlay uses injected Cypress unique selector only when it uniquely returns the selected node", () => {
  const element = target("BUTTON");
  const document = { querySelectorAll: (selector) => selector === ".fallback" ? [element] : [] };
  let calls = 0;
  globalThis.__executionV2UniqueSelector = () => {
    calls += 1;
    return ".fallback";
  };
  assert.equal(uniqueCss(document, element, {}), ".fallback");
  assert.equal(calls, 1);
  delete globalThis.__executionV2UniqueSelector;
});

test("XPath literals preserve values containing both quote types", () => {
  assert.equal(xpathLiteral(`Tom's "comment"`), `concat('Tom', "'", 's "comment"')`);
});
