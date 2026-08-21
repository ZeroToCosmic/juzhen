"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const page = require("../gateway/static/console_settings_page.js");

function node({tag = "div", name = "", value = "", type = "text", checked = false, text = ""} = {}) {
  const listeners = new Map();
  return {
    tagName: tag.toUpperCase(), name, value, type, checked, textContent: text,
    hidden: false, disabled: false, children: [], dataset: {}, attributes: {}, className: "",
    addEventListener(event, handler) { listeners.set(event, handler); },
    dispatch(event, payload = {}) { listeners.get(event)?.({...payload, target: payload.target || this, currentTarget: this, preventDefault() { this.defaultPrevented = true; }}); },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    removeAttribute(key) { delete this.attributes[key]; },
    getAttribute(key) { return this.attributes[key] ?? null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
}

function harness() {
  const root = node();
  const form = node({tag: "form"});
  form.elements = [];
  const fields = new Map();
  const byId = new Map();
  const tabs = ["network", "browser", "publishing", "r2", "collection", "models"].map((category) => {
    const item = node(); item.dataset.settingsCategory = category; return item;
  });
  const panels = ["network", "browser", "publishing", "r2", "collection", "models"].map((category) => {
    const item = node(); item.dataset.settingsPanel = category; return item;
  });
  const add = (id, value = "") => { const item = node({value}); byId.set(id, item); return item; };
  byId.set("console-settings-form", form);
  byId.set("console-settings-dirty", add("dirty"));
  byId.set("console-settings-status", add("status"));
  byId.set("console-settings-error", add("error"));
  byId.set("console-settings-save", add("save"));
  byId.set("console-settings-restore", add("restore"));
  byId.set("console-settings-refresh", add("refresh"));
  byId.set("console-settings-cookie", add("cookie"));
  byId.set("console-settings-cookie-save", add("cookie-save"));
  byId.set("console-settings-cookie-validate", add("cookie-validate"));
  byId.set("console-settings-proxy-search", add("proxy-search"));
  byId.set("console-settings-proxy-page-size", add("proxy-page-size", "50"));
  byId.set("console-settings-proxy-prev", add("proxy-prev"));
  byId.set("console-settings-proxy-next", add("proxy-next"));
  byId.set("console-settings-proxy-meta", add("proxy-meta"));
  byId.set("console-settings-proxy-body", add("proxy-body"));
  byId.set("console-settings-models", add("models"));
  byId.set("console-settings-presets", add("presets"));
  byId.set("console-settings-health", add("health"));
  byId.set("console-settings-model-default", add("default"));
  byId.set("console-settings-presets-refresh", add("presets-refresh"));
  byId.set("console-settings-cookie-status", add("cookie-status"));
  byId.set("console-settings-cookie-valid", add("cookie-valid"));
  const createElement = (tag) => node({tag});
  const lookup = (selector) => {
    if (selector.startsWith("#")) return byId.get(selector.slice(1)) || null;
    if (selector === "[data-settings-category]") return tabs;
    if (selector === "[data-settings-panel]") return panels;
    if (selector === "[data-secret-status]" || selector === "[data-model-secret-status]") return [];
    return null;
  };
  const h = {root, form, tabs, panels, fields, byId, createElement, get(id) { return byId.get(id) || null; }, querySelector: lookup, querySelectorAll: lookup};
  root.querySelector = lookup;
  root.querySelectorAll = lookup;
  return h;
}

test("settings page adapter renders every loaded model and category state", () => {
  const h = harness();
  const adapter = page.createConsoleSettingsPageAdapter({root: h.root, document: h, controller: {
    snapshot: () => ({loaded: true, healthKnown: true, canSave: true, saving: false, dirtySections: [], category: "models", message: "", error: "", settings: {
      models: {default_model_id: "m2", items: [
        {id: "m1", provider: "openai", enabled: true, base_url: "https://one", model: "gpt-4o", mode: "responses"},
        {id: "m2", provider: "anthropic", enabled: false, base_url: "https://two", model: "claude", mode: "chat"},
      ]},
      _secrets_configured: {models: {items: [{api_key: true}, {api_key: false}]}},
    }, proxyPool: {page: 1, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []}}),
    init: async () => true,
    switchCategory: () => "models",
    markDirty() {},
    destroy() {},
  }});

  assert.equal(typeof adapter.render, "function");
  adapter.render({hydrate: true});
  assert.equal(h.get("models").children.length, 2);
  assert.equal(h.get("default").value, "m2");
});

test("status renders do not overwrite an unsaved field draft", () => {
  const h = harness();
  const host = node({tag: "input", name: "proxy.host"});
  h.form.elements = [host];
  const controller = {
    snapshot: () => ({loaded: true, healthKnown: true, canSave: true, saving: false, dirtySections: ["proxy"], category: "network", message: "", error: "", settings: {proxy: {host: "saved.example"}}, proxyPool: {page: 1, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []}}),
    secretConfigured: () => false,
    switchCategory: () => "network",
    markDirty() {},
    destroy() {},
  };
  const adapter = page.createConsoleSettingsPageAdapter({root: h.root, document: h, controller});

  adapter.render({hydrate: true});
  assert.equal(host.value, "saved.example");
  host.value = "draft.example";
  adapter.render();
  assert.equal(host.value, "draft.example");
});

test("unhealthy configuration is explained without exposing the backend error", () => {
  const h = harness();
  const controller = {
    snapshot: () => ({loaded: true, healthKnown: true, canSave: false, saving: false, dirtySections: [], category: "network", message: "", error: "", settings: {}, health: {ok: false, error: "C:/private/config.json parse failed", backup_available: true}, proxyPool: {page: 1, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []}}),
    switchCategory: () => "network",
    markDirty() {},
    destroy() {},
  };
  const adapter = page.createConsoleSettingsPageAdapter({root: h.root, document: h, controller});

  adapter.render();
  assert.equal(h.get("console-settings-health").textContent, "配置文件异常，可恢复最近备份。");
  assert.doesNotMatch(h.get("console-settings-health").textContent, /private|config\.json/);
});

test("provider-keyed presets and Cookie state render from their public contracts", () => {
  const h = harness();
  const controller = {
    snapshot: () => ({loaded: true, healthKnown: true, canSave: true, saving: false, dirtySections: [], category: "models", message: "", error: "", settings: {}, presets: {grok: {label: "Grok"}, custom: {label: "自定义"}}, proxyPool: {page: 1, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []}}),
    cookieStatus: () => ({configured: true, valid: false, state: "invalid", checkedAt: "2026-08-19T10:00:00Z"}),
    switchCategory: () => "models",
    markDirty() {},
    destroy() {},
  };
  const adapter = page.createConsoleSettingsPageAdapter({root: h.root, document: h, controller});

  adapter.render();
  assert.equal(h.get("presets").textContent, "已加载 2 个模型服务预设");
  assert.equal(h.get("console-settings-cookie-status").textContent, "已配置");
  assert.equal(h.get("console-settings-cookie-valid").textContent, "验证失败 · 2026-08-19T10:00:00Z");
});

test("refreshing presets preserves an unsaved settings draft", async () => {
  const h = harness();
  const host = node({tag: "input", name: "proxy.host", value: "draft.example"});
  h.form.elements = [host];
  const calls = [];
  const controller = {
    snapshot: () => ({loaded: true, healthKnown: true, canSave: true, saving: false, dirtySections: ["proxy"], category: "models", message: "", error: "", settings: {proxy: {host: "saved.example"}}, presets: {grok: {label: "Grok"}}, proxyPool: {page: 1, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []}}),
    refreshPresets: async () => { calls.push("presets"); return true; },
    switchCategory: () => "models",
    markDirty() {},
    destroy() {},
  };
  const adapter = page.createConsoleSettingsPageAdapter({root: h.root, document: h, controller});
  adapter.bind();

  h.get("console-settings-presets-refresh").dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(calls, ["presets"]);
  assert.equal(host.value, "draft.example");
});

test("settings page adapter exposes the controller save and restore actions", async () => {
  const h = harness();
  const calls = [];
  const controller = {
    snapshot: () => ({loaded: true, healthKnown: true, canSave: true, saving: false, dirtySections: ["r2"], category: "r2", message: "", error: "", settings: {}, proxyPool: {page: 1, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []}}),
    init: async () => true,
    save: async () => { calls.push("save"); return true; },
    restoreLatest: async () => { calls.push("restore"); return true; },
    refreshProxyPool: async () => { calls.push("proxy"); return true; },
    saveCookie: async () => { calls.push("cookie"); return true; },
    validateCookie: async () => { calls.push("validate"); return true; },
    switchCategory: () => "r2",
    markDirty() {},
    destroy() {},
  };
  const adapter = page.createConsoleSettingsPageAdapter({root: h.root, document: h, controller});
  adapter.bind();
  h.form.dispatch("submit");
  h.get("console-settings-restore").dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ["save", "restore"]);
});
