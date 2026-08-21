"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const ui = require("../gateway/static/console_settings.js");

function fakeForm(values) {
  return {
    elements: Object.entries(values).map(([name, value]) => ({name, value})),
  };
}

function response(data, status = 200) {
  return {status, data};
}

function createHarness(options = {}) {
  const calls = options.calls || [];
  const unload = [];
  const responses = options.responses || {};
  const settings = options.settings || {
    proxy: {host: "127.0.0.1", port: "1080", username: "", password: ""},
    timeouts: {ip_check_seconds: 10, buffer_publish_seconds: 30},
    publish_queue: {interval_seconds: 8},
    publish_sampling: {enabled: true, interval_seconds: 300, min_age_hours: 24},
    models: {items: [{id: "primary", provider: "openai", enabled: true, api_key: ""}]},
    _secrets_configured: {proxy: {password: false}},
  };
  const requestJson = options.requestJson || (async (url, method = "GET", body) => {
    calls.push({url, method, body});
    if (url === "/api/settings" && method === "GET") return response(settings);
    if (url === "/api/settings/status") return response({ok: true});
    if (url === "/api/model-presets") return response({grok: {label: "Grok"}});
    if (url.startsWith("/api/proxy-pool/status?")) {
      return response({page: 1, page_size: 50, total: 0, assigned: 0, remaining: 0, items: []});
    }
    if (url === "/api/tiktok-stats/settings/cookie") return response({status: {configured: false}});
    if (url === "/api/settings/restore-latest") return response({settings, status: {ok: true}});
    if (url === "/api/tiktok-stats/settings/cookie/validate") return response({status: {configured: true, state: "invalid", checked_at: "2026-08-19T10:00:00Z"}});
    if (url === "/api/settings" && method === "PUT") return response(settings);
    return response({});
  });
  const controller = ui.createConsoleSettingsController({
    requestJson,
    form: options.form || fakeForm({}),
    confirm: options.confirm || (() => true),
    addBeforeUnload: (handler) => unload.push(handler),
    removeBeforeUnload: (handler) => {
      const index = unload.indexOf(handler);
      if (index >= 0) unload.splice(index, 1);
    },
  });
  return {controller, calls, unload, settings};
}

test("serialization sends only dirty top-level sections with typed values", () => {
  const form = fakeForm({
    "timeouts.buffer_publish_seconds": "45",
    "publish_queue.interval_seconds": "12",
    "publish_sampling.enabled": "false",
    "proxy.password": "",
  });
  const payload = ui.serializeDirtySections(form, {
    timeouts: {ip_check_seconds: 10, buffer_publish_seconds: 30},
    publish_queue: {interval_seconds: 8},
    publish_sampling: {enabled: true, interval_seconds: 300, min_age_hours: 24},
    _secrets_configured: {proxy: {password: true}},
  }, new Set(["timeouts", "publish_queue", "publish_sampling"]));

  assert.deepEqual(payload, {
    timeouts: {ip_check_seconds: 10, buffer_publish_seconds: 45},
    publish_queue: {interval_seconds: 12},
    publish_sampling: {enabled: false, interval_seconds: 300, min_age_hours: 24},
  });
  assert.equal("_secrets_configured" in payload, false);
  assert.equal("proxy" in payload, false);
});

test("blank secrets never become masks or explicit clears", () => {
  const payload = ui.serializeDirtySections(
    fakeForm({"r2.account_id": "acct", "r2.secret_access_key": ""}),
    {
      r2: {account_id: "old", secret_access_key: ""},
      _secrets_configured: {r2: {secret_access_key: true}},
    },
    new Set(["r2"]),
  );

  assert.equal(payload.r2.account_id, "acct");
  assert.equal(payload.r2.secret_access_key, "");
  assert.doesNotMatch(JSON.stringify(payload), /\*\*\*/);
});

test("editing models preserves every model and sends boolean enabled values", () => {
  const items = ui.mergeModelDrafts(
    [{id: "a", api_key: "", enabled: true}, {id: "b", api_key: "", enabled: false}],
    [{id: "a", api_key: "", enabled: "false"}, {id: "b", api_key: "", enabled: "true"}],
  );

  assert.deepEqual(items.map((item) => [item.id, item.enabled]), [["a", false], ["b", true]]);
  assert.equal(items.length, 2);
});

test("settings still load when health is unavailable but saving is guarded", async () => {
  const {controller} = createHarness({
    requestJson: async (url, method = "GET", body) => {
      if (url === "/api/settings") return response({proxy: {host: "127.0.0.1"}});
      if (url === "/api/settings/status") return response({error: "unavailable"}, 503);
      if (url === "/api/model-presets") return response({});
      if (url.startsWith("/api/proxy-pool/status?")) return response({total: 0, assigned: 0, remaining: 0, items: []});
      if (url === "/api/tiktok-stats/settings/cookie") return response({status: {configured: true}});
      throw new Error(`unexpected ${method} ${url} ${JSON.stringify(body)}`);
    },
  });

  await controller.init();

  assert.equal(controller.snapshot().loaded, true);
  assert.equal(controller.snapshot().healthKnown, false);
  assert.equal(controller.snapshot().canSave, false);
});

test("save rechecks health and preserves dirty sections when the check fails", async () => {
  const calls = [];
  const {controller} = createHarness({
    calls,
    form: fakeForm({"timeouts.buffer_publish_seconds": "45"}),
    requestJson: async (url, method = "GET", body) => {
      calls.push({url, method, body});
      if (url === "/api/settings") return response({timeouts: {ip_check_seconds: 10, buffer_publish_seconds: 30}});
      if (url === "/api/settings/status") return response({ok: false}, 503);
      if (url === "/api/model-presets") return response({});
      if (url.startsWith("/api/proxy-pool/status?")) return response({items: []});
      if (url === "/api/tiktok-stats/settings/cookie") return response({status: {configured: false}});
      throw new Error(`unexpected ${method} ${url} ${JSON.stringify(body)}`);
    },
  });

  await controller.init();
  controller.markDirty("timeouts");
  assert.equal(await controller.save(), false);
  assert.equal(calls.some((call) => call.method === "PUT"), false);
  assert.deepEqual(controller.snapshot().dirtySections, ["timeouts"]);
});

test("restore requires confirmation and reloads settings dependencies", async () => {
  const calls = [];
  const {controller} = createHarness({calls, confirm: () => true});

  await controller.restoreLatest();

  assert.deepEqual(calls[0], {url: "/api/settings/restore-latest", method: "POST", body: {}});
  assert.equal(calls.some((call) => call.url === "/api/model-presets"), true);
  assert.equal(calls.some((call) => call.url.startsWith("/api/proxy-pool/status?")), true);
});

test("restore is cancelled without a request", async () => {
  const calls = [];
  const {controller} = createHarness({calls, confirm: () => false});

  assert.equal(await controller.restoreLatest(), false);
  assert.equal(calls.length, 0);
});

test("blank cookie is not submitted and validation posts an empty object", async () => {
  const calls = [];
  const {controller} = createHarness({calls});

  assert.equal(await controller.saveCookie(""), false);
  await controller.validateCookie();

  assert.deepEqual(calls.at(-1), {
    url: "/api/tiktok-stats/settings/cookie/validate",
    method: "POST",
    body: {},
  });
});

test("cookie save uses the independent PUT endpoint", async () => {
  const calls = [];
  const {controller} = createHarness({calls});

  await controller.saveCookie("sessionid=secret");

  assert.deepEqual(calls.at(-1), {
    url: "/api/tiktok-stats/settings/cookie",
    method: "PUT",
    body: {cookie: "sessionid=secret"},
  });
  assert.deepEqual(controller.cookieStatus(), {configured: false, valid: null, state: "", checkedAt: null});
});

test("cookie status derives validation from the public state contract", async () => {
  const {controller} = createHarness({
    requestJson: async (url) => {
      if (url === "/api/tiktok-stats/settings/cookie/validate") return response({status: {configured: true, state: "valid", checked_at: "2026-08-19T10:00:00Z"}});
      return response({});
    },
  });

  await controller.validateCookie();

  assert.deepEqual(controller.cookieStatus(), {configured: true, valid: true, state: "valid", checkedAt: "2026-08-19T10:00:00Z"});
});

test("refreshing model presets does not reload settings or clear dirty state", async () => {
  const calls = [];
  const {controller} = createHarness({calls});
  controller.markDirty("models");

  await controller.refreshPresets();

  assert.deepEqual(calls.map((item) => item.url), ["/api/model-presets"]);
  assert.deepEqual(controller.snapshot().dirtySections, ["models"]);
  assert.deepEqual(Object.keys(controller.snapshot().presets), ["grok"]);
});

test("proxy pool requests preserve pagination and search", async () => {
  const calls = [];
  const {controller} = createHarness({calls});

  await controller.refreshProxyPool({page: 3, pageSize: 25, search: "alice user"});

  assert.equal(calls.at(-1).url, "/api/proxy-pool/status?page=3&page_size=25&search=alice%20user");
});

test("beforeunload protects dirty state and snapshot excludes secrets", async () => {
  const {controller, unload} = createHarness();
  await controller.init();
  controller.markDirty("proxy");

  const event = {preventDefault() { this.prevented = true; }};
  assert.equal(unload.length, 1);
  assert.equal(unload[0](event), "");
  assert.equal(event.prevented, true);
  assert.doesNotMatch(JSON.stringify(controller.snapshot()), /_secrets_configured|api_key|password|cookie|secret_access_key/);

  controller.destroy();
  assert.equal(unload.length, 0);
});
