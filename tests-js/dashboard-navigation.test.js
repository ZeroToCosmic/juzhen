"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {createDashboardNavigation, panelFromSearch} = require("../gateway/static/dashboard_navigation.js");

function classList() {
  const values = new Set();
  return {
    toggle(name, enabled) { enabled ? values.add(name) : values.delete(name); },
    contains(name) { return values.has(name); },
  };
}

function fixture(search = "") {
  const panels = ["settings", "accounts", "strategies"].map((name) => ({
    id: `panel-${name}`,
    classList: classList(),
  }));
  const links = ["settings", "accounts", "strategies"].map((name) => ({
    dataset: {panel: name},
    textContent: name,
    classList: classList(),
    attributes: {},
    listeners: {},
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
  }));
  const statsLink = {
    dataset: {},
    listeners: {},
    addEventListener(type, handler) { this.listeners[type] = handler; },
  };
  const historyCalls = [];
  const windowListeners = {};
  const window = {
    location: {href: `http://localhost/${search}`, search},
    history: {
      pushState(state, title, url) {
        historyCalls.push(url);
        window.location.href = url;
        window.location.search = new URL(url).search;
      },
    },
    addEventListener(type, handler) { windowListeners[type] = handler; },
  };
  const title = {textContent: ""};
  const navigation = createDashboardNavigation({window, panels, links, title});
  return {panels, links, statsLink, historyCalls, window, windowListeners, title, navigation};
}

test("panelFromSearch accepts known panels and rejects unknown values", () => {
  const allowed = new Set(["settings", "accounts"]);
  assert.equal(panelFromSearch("?panel=accounts", allowed), "accounts");
  assert.equal(panelFromSearch("?panel=missing", allowed), "settings");
  assert.equal(panelFromSearch("", allowed), "settings");
});

test("start restores requested panel and matching accessible navigation state", () => {
  const ui = fixture("?panel=accounts");
  ui.navigation.start();

  assert.equal(ui.panels[1].classList.contains("active"), true);
  assert.equal(ui.links[1].classList.contains("active"), true);
  assert.equal(ui.links[1].attributes["aria-current"], "page");
  assert.equal(ui.title.textContent, "accounts");
});

test("dashboard click changes panel and URL without page navigation", () => {
  const ui = fixture("");
  ui.navigation.start();
  let prevented = false;

  ui.links[2].listeners.click({preventDefault() { prevented = true; }});

  assert.equal(prevented, true);
  assert.equal(ui.panels[2].classList.contains("active"), true);
  assert.equal(ui.historyCalls.at(-1), "http://localhost/?panel=strategies");
});

test("popstate restores panel from current URL", () => {
  const ui = fixture("?panel=accounts");
  ui.navigation.start();
  ui.window.location.search = "?panel=strategies";

  ui.windowListeners.popstate();

  assert.equal(ui.panels[2].classList.contains("active"), true);
  assert.equal(ui.links[2].attributes["aria-current"], "page");
});

test("statistics route link remains a normal browser navigation", () => {
  const ui = fixture("");
  ui.navigation.start();

  assert.equal(ui.statsLink.listeners.click, undefined);
});
